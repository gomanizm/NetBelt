"""書けるかを調べた直後に鍵交換が始まっても、端末の大きさの送信が GUI を止めないことを検証する。

何が起きていたか（31b45ee で実測、localhost の paramiko サーバ）。1.3.2 では
鍵交換中・TCP へ書けない間は window-change を送らずに覚えておくようにしたが、
「いま書けるか」の判定（_size_watcher.check() → _transport_backlogged）と
channel.resize_pty() の書き込みは別の処理で、書き込みは GUI スレッドのまま
だった。判定が「書ける」を返した直後に Transport のスレッドが鍵交換を始めて
clear_to_send を解除すると、paramiko の _send_user_message は鍵交換が終わる
まで（最長 30 秒の時間切れまで）戻らない。
  - 接続済みの本物の Channel で、判定の直後に clear_to_send を解除し、別の
    スレッドから 3 秒後に戻す: set_terminal_size(100, 30) は 3.001 秒戻らず、
    20ms ごとの QTimer の刻みの最大の間隔も 3.021 秒（その間は切断の操作も
    受け付けない）。大きさは戻した時点で機器へ届いた
  - 判定では書けるのに書き込みが待たされる（偽のチャネルの resize_pty が
    2 秒戻らない）: set_terminal_size は 2.00 秒戻らない
441ea02 は判定そのものが無く、鍵交換中ならいつでも止まっていた（同じ
localhost の構成で、鍵交換中に呼ぶと 3.000 秒戻らない）。

どう直したか。window-change の書き込みを GUI スレッドの外へ出した。書くのは
打鍵・貼り付けのデータと同じ書き手のスレッド（_ChannelWriter。チャネルごとに
1 つで、渡された順に書く）。GUI スレッドは大きさを渡すだけで、書き終わりは
短く（_WRITE_WAIT_SECONDS の 0.1 秒まで）しか待たない。ふだんは 1ms も
かからずに書き終わるので、これまでどおり大きさを伝えてから次の打鍵を送る
順序になる。書き込みが待たされている間に変わった大きさは、前の書き込みが
終わってから最後の 1 つだけを書く。書く前の判定と見張り（鍵交換中・TCP へ
書けない間は送らずに覚えておき、書けるようになったら最後の大きさを 1 回
送る）は今までどおり。後始末（dispose）では書き手も止め、切断・繋ぎ直しの
あとに古い接続へ書かない（書き手は作ったときのチャネルにだけ書く）。直した
あとの同じ測り方（3 秒、1d9d5c5 で測った）: set_terminal_size は 0.101 秒で
戻り、刻みの最大の間隔は 0.121 秒、大きさは鍵交換が終わった 3.001 秒後に
届いた。
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
# set_terminal_size が戻るまでの許容（書き手を待つのは 0.1 秒まで）
RETURN_LIMIT_SECONDS = 0.5


class _FakeTransport:
    def __init__(self, sock):
        self.sock = sock
        self.clear_to_send = threading.Event()
        self.clear_to_send.set()


class _WaitingChannel:
    """resize_pty が release まで戻らないチャネル（偽物）

    判定では書けたのに、書き込みで待たされる（判定の直後に鍵交換が始まった）
    ことに当たる。
    """

    closed = False
    out_window_size = 1024 * 1024

    def __init__(self, transport):
        self.transport = transport
        self.release = threading.Event()
        self.resizes = []

    def get_transport(self):
        return self.transport

    def resize_pty(self, width, height):
        self.release.wait(10)
        self.resizes.append((width, height))

    def close(self):
        self.closed = True


class _ShellServer(paramiko.ServerInterface):
    def __init__(self, resizes):
        self.shell = threading.Event()
        self.resizes = resizes

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

    def check_channel_window_change_request(self, channel, width, height,
                                            pixelwidth, pixelheight):
        self.resizes.append((width, height))
        return True


class _SSHServer:
    """受信したものを読み捨てる localhost のシェル（1 接続だけ受ける）"""

    def __init__(self):
        self.resizes = []
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
        server = _ShellServer(self.resizes)
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

    def _timed_resize(self, conn, cols, rows):
        started = time.perf_counter()
        conn.set_terminal_size(cols, rows)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, RETURN_LIMIT_SECONDS,
                        "set_terminal_size(%d, %d) が %.2f 秒 GUI スレッドを止めた"
                        % (cols, rows, elapsed))


class WaitingWriteTest(_QtCase):
    """判定では書けたのに、書き込みが待たされる場合（偽のチャネル）"""

    def _session(self):
        from core.send_backpressure import DrainWatcher
        from core.ssh_connection import SSHConnection
        a, b = socket.socketpair()      # 待たずに書ける（詰めていない）
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        channel = _WaitingChannel(_FakeTransport(a))
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
        return conn, channel

    def test_a_write_that_waits_does_not_hold_the_gui_thread(self):
        conn, channel = self._session()

        self._timed_resize(conn, 100, 30)

    def test_sizes_changed_while_a_write_waits_end_with_the_last_one(self):
        conn, channel = self._session()
        self._timed_resize(conn, 100, 30)
        self._timed_resize(conn, 120, 40)
        self._timed_resize(conn, 132, 43)

        channel.release.set()           # 待たされていた書き込みが終わる
        self._pump(3, until=lambda: (132, 43) in channel.resizes)
        self._pump(0.3)

        self.assertEqual([(100, 30), (132, 43)], channel.resizes,
                         "待たされている間に変わった大きさが、最後の 1 つだけ"
                         "・最後に書かれていない")


class KeyExchangeStartsAfterTheCheckTest(_QtCase):
    """本物の paramiko で、判定の直後に鍵交換が始まる場合"""

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshresizekex-"))
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
        """次に「Transport へ書ける」と判定された直後に鍵交換を始める

        clear_to_send を解除し（paramiko が鍵交換を始めるときと同じ）、
        HOLD_SECONDS 後に別のスレッドから戻す。戻す Event を返す。
        """
        from core.ssh_connection import SSHConnection
        transport = conn.client.get_transport()
        original = SSHConnection._transport_backlogged
        fired = threading.Event()

        def check_then_start_kex(channel):
            result = original(channel)
            if not result and not fired.is_set():
                fired.set()
                transport.clear_to_send.clear()
            return result

        patcher = mock.patch.object(SSHConnection, "_transport_backlogged",
                                    staticmethod(check_then_start_kex))
        patcher.start()
        self.addCleanup(patcher.stop)
        timer = threading.Timer(HOLD_SECONDS, transport.clear_to_send.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.addCleanup(transport.clear_to_send.set)
        return fired, transport

    def test_the_gui_keeps_running_and_the_size_arrives_after_the_exchange(self):
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
        self._timed_resize(conn, 100, 30)
        self.assertTrue(fired.is_set(), "前提: 判定の直後に鍵交換を始められていない")
        self._pump(HOLD_SECONDS + 3, until=lambda: server.resizes)
        arrived = time.perf_counter() - started
        self._pump(0.3)

        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertTrue(gaps, "前提: GUI の刻みが測れていない")
        self.assertLess(max(gaps), 1.0,
                        "GUI の刻みが %.2f 秒止まった" % max(gaps))
        self.assertEqual([(100, 30)], server.resizes,
                         "鍵交換が終わったあとに大きさが届いていない")
        self.assertGreater(arrived, HOLD_SECONDS - 0.5,
                           "前提: 鍵交換中に window-change が書けてしまった")
        self.assertEqual([], errors)
        self.assertEqual([], closed)

    def test_no_size_reaches_the_old_connection_after_reconnecting(self):
        from core.ssh_connection import SSHConnection
        old = _SSHServer()
        self.addCleanup(old.close)
        new = _SSHServer()
        self.addCleanup(new.close)
        conn = SSHConnection("127.0.0.1", old.port, "u", "p")
        self.addCleanup(conn.dispose)
        errors, closed = self._connect(conn, old)
        fired, old_transport = self._start_key_exchange_after_the_check(conn)
        self._timed_resize(conn, 100, 30)
        self.assertTrue(fired.is_set(), "前提: 判定の直後に鍵交換を始められていない")

        started = time.perf_counter()
        conn.disconnect()               # 書き込みが鍵交換を待っている間に切る
        self.assertLess(time.perf_counter() - started, 1.0,
                        "切断が送信の終わりを待った")
        conn.port = new.port
        self.assertTrue(conn.connect(), "前提: 別の機器へ繋ぎ直せない")
        self.assertTrue(new.ready.wait(5))
        self._timed_resize(conn, 90, 20)
        old_transport.clear_to_send.set()     # 古い接続の鍵交換が終わる
        self._pump(3, until=lambda: new.resizes)
        self._pump(0.5)

        self.assertEqual([], old.resizes,
                         "切断したあとの古い接続へ window-change を書いた")
        self.assertEqual([(90, 20)], new.resizes,
                         "繋ぎ直した先へ新しい大きさが届いていない")
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
