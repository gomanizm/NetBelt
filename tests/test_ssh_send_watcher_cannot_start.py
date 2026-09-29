"""送信の見張り（netbelt-send-drain）を始められなくても、SSH の入力が止まらず、後始末が Transport を閉じることを検証する。

何が起きていたか（958475e で実測。localhost の paramiko サーバ（チャネルを
読まない）へ繋ぎ、threading.Thread.start を、名前が netbelt-send-drain の
スレッドだけ RuntimeError にした）。DrainWatcher.check は、見張りのスレッドを
見張り中の印（_thread）に入れてから start していたので、開始に失敗すると、
始まっていないスレッドが印に残ったまま例外が出た。以後の check は「見張り中」と
見て見張りを始めず、知らせ（send_drained）も出ない。端末の大きさの見張りは
4b195db でこの失敗を扱うようにしたが、データの見張り（_write_carry・
has_pending_sends）は扱っていなかった。
  - 鍵交換中に set_terminal_size(132, 43)（大きさの見張りも始められず、GUI の
    タイマーで調べ直す）→ 鍵交換を終わらせる → Qt のイベントを回さずに
    send_command('x'): 保留した大きさのせいでデータの見張りを始めようとして、
    send_command が RuntimeError を投げた。3 秒回すと window-change は届いたが、
    'x' は持ち越し（_carry）に残ったまま機器へ届かず、send_drained は 1 回も
    出なかった
  - 端末（MainWindow と同じ配線）から 'x' と 'y' を打つと、'x' で RuntimeError、
    'y' は「待たされている」として端末の列に残った。send_drained が来ないので
    3 秒後も機器へは何も届かず、そのあと打った 'z' も列に積まれるだけだった
    （端末の入力が黙って止まったまま）
  - そのあとの dispose は、始まっていないスレッドを join して RuntimeError
    （cannot join thread before it is started）になり、client.close まで
    進まなかった。Transport は動いたままで、機器側のセッションも閉じなかった
DrainWatcher のこの弱点は 441ea02 からある（Telnet も同じ。
test_telnet_send_watcher_cannot_start.py）。

どう直したか。DrainWatcher.check は、開始できなかったスレッドを印に残さずに
例外を出す（次の check でまた始められる）。stop は動いていないスレッドを
join しない。SSH のデータ側は、見張りを始められないときも「待たずには書けない」
として入力を待たせ、GUI スレッドのタイマーで（見張りと同じ間隔で。予約は
いつも 1 本だけ）調べ直し、書けるようになったら send_drained を出して続きを
書かせる。調べ直しのたびに見張りの開始も試す。保留した大きさがある間は
これまでどおり待たせるので、window-change を打鍵が追い越さない。GUI は待たない。
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

_real_start = threading.Thread.start


def _drain_watcher_cannot_start(thread):
    """送信の見張り（netbelt-send-drain）の開始だけを失敗させる（どのスレッドからでも）"""
    if thread.name == "netbelt-send-drain":
        raise RuntimeError("can't start new thread")
    return _real_start(thread)


class DrainWatcherStartFailureTest(unittest.TestCase):
    """DrainWatcher だけで確かめる（Qt を使わない）"""

    def test_a_later_check_starts_the_watcher_after_a_failed_start(self):
        from core.send_backpressure import DrainWatcher
        busy = threading.Event()
        busy.set()
        notified = threading.Event()
        watcher = DrainWatcher(busy.is_set, notified.set)
        self.addCleanup(watcher.stop)
        with mock.patch.object(threading.Thread, "start",
                               _drain_watcher_cannot_start):
            with self.assertRaises(RuntimeError):
                watcher.check()   # 開始の失敗は呼び出し側へ知らせる

        self.assertTrue(watcher.check(), "前提: まだ待たずには書けない")
        busy.clear()
        self.assertTrue(notified.wait(3),
                        "開始に失敗した見張りが残り、次の check で見張りが"
                        "始まらない（書けるようになっても知らせが来ない）")

    def test_stop_after_a_failed_start_does_not_raise(self):
        from core.send_backpressure import DrainWatcher
        watcher = DrainWatcher(lambda: True, lambda: None)
        with mock.patch.object(threading.Thread, "start",
                               _drain_watcher_cannot_start):
            with self.assertRaises(RuntimeError):
                watcher.check()
        try:
            watcher.stop()
        except RuntimeError as e:
            self.fail("開始に失敗した見張りを止めると例外になる（後始末が"
                      "途中で止まる）: %s" % e)
        self.assertFalse(watcher.check(), "止めた後も待たせる")


class _ShellServer(paramiko.ServerInterface):
    def __init__(self, events):
        self.shell = threading.Event()
        self.events = events

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
        # Transport のスレッドは届いた順に処理するので、この時点でチャネルに
        # 溜まっている量が、window-change より先に届いたデータ
        self.events.append((width, height, len(channel.in_buffer)))
        return True


class _SilentSSHServer:
    """チャネルを読まない localhost のシェル（届いたデータは in_buffer に残る）"""

    def __init__(self):
        self.resizes = []      # (幅, 高さ, それより先に届いたデータのバイト数)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.ready = threading.Event()
        self.transport = None
        self.channel = None
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
        self.channel = ch
        self.ready.set()

    def received(self) -> int:
        return len(self.channel.in_buffer) if self.channel is not None else 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        if self.transport is not None:
            self.transport.close()


class SSHSendWatcherCannotStartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshdrainstart-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=d)
        patcher.start()
        self.addCleanup(patcher.stop)
        # アプリの excepthook（main.install_excepthook）と同じく、Qt から
        # 呼ばれた処理の例外を記録して続ける（既定のままだと PyQt が落とす）
        self.hooked = []
        hook = mock.patch.object(
            sys, "excepthook",
            lambda etype, value, tb: self.hooked.append(
                "%s: %s" % (etype.__name__, value)))
        hook.start()
        self.addCleanup(hook.stop)

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _session(self):
        """localhost へ繋ぎ、以後は送信の見張りのスレッドを始められないようにする"""
        from core.ssh_connection import SSHConnection
        server = _SilentSSHServer()
        self.addCleanup(server.close)
        conn = SSHConnection("127.0.0.1", server.port, "u", "p")
        self.addCleanup(conn.dispose)
        self.errors, self.closed, self.drained = [], [], []
        conn.error_occurred.connect(self.errors.append)
        conn.disconnected.connect(lambda: self.closed.append(True))
        conn.send_drained.connect(lambda: self.drained.append(True))
        self.assertTrue(conn.connect(), "前提: localhost の SSH に繋がる")
        self.assertTrue(server.ready.wait(5))
        self._pump(0.2)
        transport = conn.client.get_transport()
        # 鍵交換が終わらないまま止まっても、テストの後始末で必ず戻す
        self.addCleanup(transport.clear_to_send.set)
        patcher = mock.patch.object(threading.Thread, "start",
                                    _drain_watcher_cannot_start)
        patcher.start()
        self.addCleanup(patcher.stop)
        return conn, server, transport

    def _hold_a_size_until_the_transport_recovers(self, conn, server, transport):
        """鍵交換中に大きさを変え、鍵交換を終わらせる（大きさの調べ直しはまだ）"""
        transport.clear_to_send.clear()          # 鍵交換中
        conn.set_terminal_size(132, 43)
        self.assertEqual([], server.resizes, "前提: 鍵交換中に window-change を書いた")
        self.assertTrue(conn._size_unsent, "前提: 大きさが保留されていない")
        transport.clear_to_send.set()            # 鍵交換が終わる

    def test_a_key_after_the_transport_recovers_still_goes_out(self):
        """Transport が戻った直後、大きさの調べ直しより先に来た打鍵（Codex の再現手順）"""
        conn, server, transport = self._session()
        self._hold_a_size_until_the_transport_recovers(conn, server, transport)
        raised = []
        try:
            conn.send_command("x")               # Qt のイベントを処理する前の打鍵
        except RuntimeError as e:
            raised.append(e)
        self._pump(5, until=lambda: server.received() >= 1
                   or self.errors or self.closed)
        self._pump(0.2)

        self.assertEqual(1, server.received(),
                         "見張りを始められないと、打鍵が持ち越しに残ったまま"
                         "機器へ届かない")
        self.assertEqual([(132, 43, 0)], server.resizes,
                         "打鍵が window-change を追い越した（3 つ目は "
                         "window-change より先に届いたバイト数）")
        self.assertEqual([], raised,
                         "send_command が見張りの開始の失敗をそのまま投げた")
        self.assertTrue(self.drained, "書けるようになっても send_drained が出ない")
        self.assertEqual([], self.errors)
        self.assertEqual([], self.closed)
        self.assertFalse(conn.has_pending_sends(),
                         "送り終えたのに、端末に次を渡させない")
        self.assertEqual([], self.hooked)

    def test_the_terminal_keeps_sending_when_the_watcher_cannot_start(self):
        """端末の送信の列を通しても、入力が止まったままにならない"""
        from ui.terminal_widget import TerminalWidget
        conn, server, transport = self._session()
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        term.key_pressed.connect(conn.send_command)
        term.set_send_backlog(conn.has_pending_sends)
        conn.send_drained.connect(term.resume_send_queue)

        self._hold_a_size_until_the_transport_recovers(conn, server, transport)
        raised = []
        for key in "xy":
            try:
                term.send_text(key)
            except RuntimeError as e:
                raised.append(e)
        self._pump(5, until=lambda: server.received() >= 2
                   or self.errors or self.closed)
        self._pump(0.2)

        self.assertEqual(2, server.received(),
                         "見張りを始められないと、端末の入力が止まったまま"
                         "（列に残り、send_drained が来ない）")
        self.assertEqual(b"xy", server.channel.in_buffer.read(64, 1))
        self.assertEqual([(132, 43, 0)], server.resizes,
                         "端末から渡した打鍵が、保留していた window-change を追い越した")

        term.send_text("z")                      # 詰まりが解けたあとの打鍵
        self._pump(3, until=lambda: server.received() >= 1)
        self.assertEqual(b"z", server.channel.in_buffer.read(64, 1),
                         "詰まりが解けたあとの打鍵が届かない")
        self.assertEqual([], term._send_queue, "端末の列に残った")
        self.assertEqual([], raised,
                         "端末からの送信が見張りの開始の失敗をそのまま投げた")
        self.assertEqual([], self.errors)
        self.assertEqual([], self.closed)
        self.assertEqual([], self.hooked)

    def test_repeated_keys_keep_a_single_retry(self):
        """見張りを始められない間に何度打っても、調べ直しは 1 本だけで、詰まりが解ければ全部届く"""
        from core.send_backpressure import DrainWatcher
        conn, server, transport = self._session()
        polls = [0]
        backlogged = conn._send_backlogged

        def counting_backlogged(channel):
            polls[0] += 1
            return backlogged(channel)
        conn._send_backlogged = counting_backlogged

        transport.clear_to_send.clear()          # 鍵交換中（データは待たされる）
        raised = []
        for _ in range(100):
            try:
                conn.send_command("k")
            except RuntimeError as e:
                raised.append(e)
            self.app.processEvents()
        polls[0] = 0
        window = 0.3
        self._pump(window)
        polled = polls[0]

        transport.clear_to_send.set()            # 鍵交換が終わる
        self._pump(5, until=lambda: server.received() >= 100
                   or self.errors or self.closed)
        self._pump(0.1)
        polls[0] = 0
        self._pump(0.3)
        polled_after = polls[0]

        self.assertEqual(100, server.received(),
                         "見張りを始められないと、鍵交換のあとも打鍵が届かない")
        self.assertEqual(b"k" * 100, server.channel.in_buffer.read(200, 1))
        limit = int(window / DrainWatcher.POLL_SECONDS) + 10
        self.assertLessEqual(polled, limit,
                             "見張りを始められない間、打鍵のたびに調べ直しが増えた")
        self.assertEqual(0, polled_after, "送り終えたあとも、調べ直しが続いている")
        self.assertEqual([], raised)
        self.assertEqual([], self.errors)
        self.assertEqual([], self.hooked)

    def test_dispose_closes_the_transport_when_the_watcher_cannot_start(self):
        """見張りを始められず、調べ直しを待っている間に閉じても、Transport を閉じる"""
        conn, server, transport = self._session()
        self._hold_a_size_until_the_transport_recovers(conn, server, transport)
        try:
            conn.send_command("x")
        except RuntimeError:
            pass   # アプリでは excepthook が記録して続ける（main.install_excepthook）
        raised = []
        try:
            conn.dispose()
        except RuntimeError as e:
            raised.append(e)
        drained_before = len(self.drained)
        self._pump(3, until=lambda: not server.transport.is_active())
        self._pump(0.3)                          # 予約済みの調べ直しが来る

        self.assertFalse(transport.is_active(),
                         "見張りを始められなかったあと、後始末で Transport が閉じない")
        self.assertFalse(server.transport.is_active(),
                         "後始末のあとも機器側のセッションが残っている")
        self.assertEqual([], raised, "後始末が例外で途中で止まった")
        self.assertEqual(drained_before, len(self.drained),
                         "後始末のあとに send_drained が出た")
        self.assertEqual(0, server.received(), "後始末のあとに打鍵を書いた")
        self.assertEqual([], self.hooked)


if __name__ == "__main__":
    unittest.main()
