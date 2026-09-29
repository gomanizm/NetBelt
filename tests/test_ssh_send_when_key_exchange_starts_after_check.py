"""書けるかを調べた直後に鍵交換が始まっても、打鍵・貼り付けの送信が GUI を止めないことを検証する。

何が起きていたか（30a3821 と 441ea02 で実測、localhost の paramiko サーバ）。
送信（send_command → _write_carry）は、「いま書けるか」の判定
（_drain_watcher.check() → _send_backlogged: 鍵交換中でない・窓が空いている・
TCP へ書ける）を見てから、GUI スレッドで channel.sendall() していた。判定が
「書ける」を返した直後に Transport のスレッドが鍵交換を始めて clear_to_send を
解除すると、paramiko の _send_user_message は鍵交換が終わるまで（最長 30 秒の
時間切れまで）戻らない。window-change では 30a3821 で直したのと同じ窓で、
打鍵・貼り付けの区切り・マクロの 1 行ごとに開く。
  - 接続済みの本物の Channel で、判定の直後に clear_to_send を解除し、別の
    スレッドから 2 秒後に戻す: send_command('x') は 2.001 秒戻らない
  - 検査役の本物の鍵交換（判定の直後に機器が KEXINIT を送り、応答を 3 秒
    遅らせる）: send_command('x') は 3.002 秒戻らず、20ms ごとの QTimer の
    刻みの最大の間隔も 3.005 秒（441ea02 も 3.002 秒・3.024 秒）。その間は
    切断の操作も受け付けない。データは鍵交換が終わった時点で届いた

どう直したか。チャネルへの書き込み（データと window-change）を、チャネル
ごとの 1 本の書き手のスレッドへ出した。GUI スレッドは書き手に渡して、書き
終わりを短く（0.1 秒まで）しか待たない。ふだんは 1ms もかからずに書き
終わるので、これまでどおりその場で書いたのと同じ順序・同じ知らせになる。
書き終わらないうちは「待たずには書けない」（has_pending_sends）として端末に
次を渡させず、後から来た送信は持ち越しの後ろへ足す（順序は崩れない）。
書き終われば見張りが send_drained で知らせ、続きを書く。書き手で起きた
失敗は、GUI スレッドがこれまでどおり『送信エラー』として知らせる。後始末
（dispose）では書き手も止め、切断・繋ぎ直しのあとに古い接続へ書かない。
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402

# 鍵交換が終わらずに待たされる時間。直す前はこの間 GUI スレッドが止まる
HOLD_SECONDS = 2.0
# send_command が戻るまでの許容（書き手を待つのは 0.1 秒まで）
RETURN_LIMIT_SECONDS = 0.5


class _FakeTransport:
    def __init__(self, sock):
        self.sock = sock
        self.clear_to_send = threading.Event()
        self.clear_to_send.set()


class _WaitingChannel:
    """最初の sendall が release まで戻らないチャネル（偽物）

    判定では書けたのに、書き込みで待たされる（判定の直後に鍵交換が始まった）
    ことに当たる。書いた順に writes へ控える。fail を渡すと、待ったあとに
    その例外を投げる。
    """

    closed = False
    out_window_size = 1024 * 1024

    def __init__(self, transport, fail=None):
        self.transport = transport
        self.release = threading.Event()
        self.fail = fail
        self.waited = False
        self.writes = []

    def get_transport(self):
        return self.transport

    def sendall(self, data):
        if not self.waited:
            self.waited = True
            self.release.wait(10)
            if self.fail is not None:
                raise self.fail
        self.writes.append(("data", bytes(data)))

    def resize_pty(self, width, height):
        self.writes.append(("size", (width, height)))

    def close(self):
        self.closed = True


class _ShellServer(paramiko.ServerInterface):
    def __init__(self):
        self.shell = threading.Event()

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True

    def check_channel_window_change_request(self, *args):
        return True


class _SSHServer:
    """受け取った分を received に控える localhost のシェル（1 接続だけ受ける）"""

    def __init__(self):
        self.received = bytearray()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.transport = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            accepted, _ = self.sock.accept()
        except OSError:
            return
        t = paramiko.Transport(accepted)
        self.transport = t
        t.add_server_key(paramiko.ECDSAKey.generate())
        server = _ShellServer()
        try:
            t.start_server(server=server)
        except Exception:
            return
        ch = t.accept(10)
        if ch is None or not server.shell.wait(10):
            return
        ch.settimeout(0.2)
        self.ready.set()
        while not self.stop.is_set():
            try:
                d = ch.recv(65536)
            except socket.timeout:
                continue
            except Exception:
                break
            if not d:
                break
            self.received.extend(d)

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self.transport is not None:
            self.transport.close()


class _QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _timed_send(self, conn, text):
        started = time.perf_counter()
        conn.send_command(text)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, RETURN_LIMIT_SECONDS,
                        "send_command(%r) が %.2f 秒 GUI スレッドを止めた"
                        % (text, elapsed))


class WaitingWriteTest(_QtCase):
    """判定では書けたのに、書き込みが待たされる場合（偽のチャネル）"""

    def _session(self, fail=None):
        from core.send_backpressure import DrainWatcher
        from core.ssh_connection import SSHConnection
        a, b = socket.socketpair()      # 待たずに書ける（詰めていない）
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        channel = _WaitingChannel(_FakeTransport(a), fail=fail)
        # 直す前は GUI スレッドが待ち続けるので、別スレッドで必ず戻す
        timer = threading.Timer(HOLD_SECONDS, channel.release.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.addCleanup(channel.release.set)
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.channel = channel
        conn.is_connected = True
        conn._drain_watcher = DrainWatcher(
            lambda: conn._send_backlogged(channel), conn._announce_drained)
        self.addCleanup(conn.dispose)
        errors, drained = [], []
        conn.error_occurred.connect(errors.append)
        conn.send_drained.connect(lambda: drained.append(True))
        return conn, channel, errors, drained

    def test_a_write_that_waits_does_not_hold_the_gui_thread(self):
        conn, channel, errors, _ = self._session()

        self._timed_send(conn, "x")

        self.assertTrue(conn.has_pending_sends(),
                        "書き込みが終わっていないのに、端末に次の区切りを渡させる")
        self.assertEqual([], errors)

    def test_later_sends_follow_the_waiting_write_in_order(self):
        conn, channel, errors, drained = self._session()
        self._timed_send(conn, "x")
        self._timed_send(conn, "yz")
        conn.set_terminal_size(100, 30)

        channel.release.set()           # 待たされていた書き込みが終わる
        self._pump(3, until=lambda: drained and not conn.has_pending_sends())
        self._pump(0.3)

        self.assertEqual([("data", b"x"), ("size", (100, 30)), ("data", b"yz")],
                         channel.writes,
                         "待たされた書き込みのあとの送信が、渡した順に書かれていない")
        self.assertTrue(drained, "書き終わったことが send_drained で知らされていない")
        self.assertFalse(conn.has_pending_sends())
        self.assertEqual([], errors)

    def test_a_failure_after_the_wait_is_reported_once(self):
        """鍵交換が終わらないまま時間切れで失敗しても、黙らずに知らせる"""
        conn, channel, errors, _ = self._session(
            fail=OSError("connection reset"))
        self._timed_send(conn, "x")
        self._timed_send(conn, "y")

        # 鍵交換が終わらないまま（clear_to_send は解除されたまま）書き込みが失敗する
        channel.transport.clear_to_send.clear()
        channel.release.set()
        self._pump(3, until=lambda: errors)
        self._pump(0.3)

        self.assertEqual(["送信エラー: connection reset"], errors,
                         "待たされたあとの書き込みの失敗が、1 回だけ知らされていない")
        self.assertEqual([], channel.writes, "失敗のあとに続きを書いた")
        self.assertEqual(b"", conn._carry, "失敗のあとも書き残しが残った")


class _FastChannel(_WaitingChannel):
    """待たずに書けるチャネル（偽物）"""

    def __init__(self, transport):
        super().__init__(transport)
        self.waited = True


_real_start = threading.Thread.start


def _start_fails_on_the_gui_thread(thread):
    """GUI スレッドからのスレッドの開始だけを失敗させる（ほかのスレッドには効かせない）"""
    if threading.current_thread() is threading.main_thread():
        raise RuntimeError("can't start new thread")
    return _real_start(thread)


class WriterOrderTest(unittest.TestCase):
    """書き手のスレッドが動き出す前に大きさも渡されたとき、渡した順に書く"""

    def test_data_handed_before_a_size_is_written_first(self):
        from core.ssh_connection import _ChannelWriter
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        channel = _FastChannel(_FakeTransport(a))
        writer = _ChannelWriter(channel)
        deferred = []

        def defer_start_on_the_gui_thread(thread):
            if threading.current_thread() is threading.main_thread():
                deferred.append(thread)    # まだ動き出さない（混んでいる）
                return None
            return _real_start(thread)

        with mock.patch.object(threading.Thread, "start",
                               defer_start_on_the_gui_thread):
            self.assertFalse(writer.write(b"x", 0.0),
                             "前提: 書き手がまだ書いていない")
            writer.send_size(100, 30, 0.0)
        self.assertEqual(1, len(deferred), "前提: 書き手のスレッドは 1 本")
        _real_start(deferred[0])
        deferred[0].join(2)

        self.assertEqual([("data", b"x"), ("size", (100, 30))], channel.writes,
                         "先に渡したデータより先に、後から渡した大きさを書いた")
        self.assertFalse(writer.busy())


class ThreadCannotStartTest(_QtCase):
    def test_data_is_written_in_place_when_no_thread_can_start(self):
        from core.send_backpressure import DrainWatcher
        from core.ssh_connection import SSHConnection
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        channel = _FastChannel(_FakeTransport(a))
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.channel = channel
        conn.is_connected = True
        conn._drain_watcher = DrainWatcher(
            lambda: conn._send_backlogged(channel), conn._announce_drained)
        self.addCleanup(conn.dispose)

        with mock.patch.object(threading.Thread, "start",
                               _start_fails_on_the_gui_thread):
            conn.send_command("x")
        conn.send_command("y")
        self._pump(0.3)

        self.assertEqual([("data", b"x"), ("data", b"y")], channel.writes,
                         "スレッドを作れないと、送った文字が機器へ届かない")


class KeyExchangeStartsAfterTheCheckTest(_QtCase):
    """本物の paramiko で、判定の直後に鍵交換が始まる場合"""

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshsendkex-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=d)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connect(self, conn, server):
        errors, closed = [], []
        conn.error_occurred.connect(errors.append)
        conn.disconnected.connect(lambda: closed.append(True))
        self.assertTrue(conn.connect(), "前提: localhost の SSH に繋がる")
        self.assertTrue(server.ready.wait(5))
        self._pump(0.2)
        return errors, closed

    def _start_key_exchange_after_the_check(self, conn):
        """次に「待たずに書ける」と判定された直後に鍵交換を始める

        clear_to_send を解除し（paramiko が鍵交換を始めるときと同じ）、
        HOLD_SECONDS 後に別のスレッドから戻す。
        """
        from core.ssh_connection import SSHConnection
        transport = conn.client.get_transport()
        original = SSHConnection._send_backlogged
        fired = threading.Event()

        def check_then_start_kex(self_, channel):
            result = original(self_, channel)
            if not result and not fired.is_set():
                fired.set()
                transport.clear_to_send.clear()
            return result

        patcher = mock.patch.object(SSHConnection, "_send_backlogged",
                                    check_then_start_kex)
        patcher.start()
        self.addCleanup(patcher.stop)
        timer = threading.Timer(HOLD_SECONDS, transport.clear_to_send.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.addCleanup(transport.clear_to_send.set)
        return fired, transport

    def test_the_gui_keeps_running_and_the_data_arrives_in_order(self):
        from PyQt6.QtCore import QTimer
        from core.ssh_connection import SSHConnection
        server = _SSHServer()
        self.addCleanup(server.close)
        conn = SSHConnection("127.0.0.1", server.port, "u", "p")
        self.addCleanup(conn.dispose)
        errors, closed = self._connect(conn, server)
        fired, _ = self._start_key_exchange_after_the_check(conn)
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
        timer.start(20)
        self.addCleanup(timer.stop)

        started = time.perf_counter()
        self._timed_send(conn, "x")
        self.assertTrue(fired.is_set(), "前提: 判定の直後に鍵交換を始められていない")
        self.assertTrue(conn.has_pending_sends(),
                        "鍵交換を待つ書き込みがあるのに、端末に次の区切りを渡させる")
        self._pump(0.5)
        self._timed_send(conn, "abc\r")     # 鍵交換の間に打った分
        self._pump(HOLD_SECONDS + 3,
                   until=lambda: bytes(server.received) == b"xabc\r")
        arrived = time.perf_counter() - started
        self._pump(0.3)

        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertTrue(gaps, "前提: GUI の刻みが測れていない")
        self.assertLess(max(gaps), 1.0,
                        "GUI の刻みが %.2f 秒止まった" % max(gaps))
        self.assertEqual(b"xabc\r", bytes(server.received),
                         "鍵交換のあとに、打った順に 1 回ずつ届いていない")
        self.assertGreater(arrived, HOLD_SECONDS - 0.5,
                           "前提: 鍵交換中にデータが書けてしまった")
        self.assertFalse(conn.has_pending_sends())
        self.assertEqual([], errors)
        self.assertEqual([], closed)

    def test_nothing_reaches_either_connection_wrongly_after_reconnecting(self):
        from core.ssh_connection import SSHConnection
        old = _SSHServer()
        self.addCleanup(old.close)
        new = _SSHServer()
        self.addCleanup(new.close)
        conn = SSHConnection("127.0.0.1", old.port, "u", "p")
        self.addCleanup(conn.dispose)
        errors, closed = self._connect(conn, old)
        fired, old_transport = self._start_key_exchange_after_the_check(conn)
        self._timed_send(conn, "x")
        self.assertTrue(fired.is_set(), "前提: 判定の直後に鍵交換を始められていない")
        self._timed_send(conn, "y")         # 持ち越しに残る

        started = time.perf_counter()
        conn.disconnect()               # 書き込みが鍵交換を待っている間に切る
        self.assertLess(time.perf_counter() - started, 1.0,
                        "切断が送信の終わりを待った")
        conn.port = new.port
        self.assertTrue(conn.connect(), "前提: 別の機器へ繋ぎ直せない")
        self.assertTrue(new.ready.wait(5))
        self._timed_send(conn, "new")
        old_transport.clear_to_send.set()     # 古い接続の鍵交換が終わる
        self._pump(3, until=lambda: bytes(new.received) == b"new")
        self._pump(0.5)

        self.assertEqual(b"", bytes(old.received),
                         "切断したあとの古い接続へ書いた")
        self.assertEqual(b"new", bytes(new.received),
                         "繋ぎ直した先へ、古い接続の送信が流れた（または新しい送信が届かない）")
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
