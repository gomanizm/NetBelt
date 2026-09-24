"""読むのが遅い機器へ SSH で大きく貼り付けても、セッションが切れないことを検証する。

何が起きていたか（基準 028ebc2 で実測）。端末は貼り付けを 512 文字ずつ
key_pressed で渡し、SSHConnection.send_command は GUI スレッドで
channel.sendall していた。チャネルには受信のために settimeout(0.1) が
掛かっており、paramiko は相手の受信ウィンドウが 0 のまま 0.1 秒過ぎると
socket.timeout を投げる。本文の空な『送信エラー: 』として出て、MainWindow は
切断として扱う。sendall はどこまで送れたかを返さないので、続きから送り直す
こともできなかった。
  - 利用者の実機（CML の IOSv へ SSH、2026-09-24）: 「!」で始まるコメント行だけの
    16KB（219 行）を貼り付けると切れ、機器に届いたのは 111 行目まで。
  - localhost（受信ウィンドウ 32768、1 秒に約 1,000 バイト読む相手）: 16KB は
    通り、48KB は 0.11 秒後に『送信エラー: 』で切断。
  - このファイルの相手（ウィンドウ 16384、16KB 読むごとに 0.25 秒休む）へ
    96KB（98,236 バイト）: 約 0.2 秒で『送信エラー: 』。そのとき相手が
    読めていたのは 16,384 バイト。直した後は約 1.8 秒で全部が順序どおりに
    届き、エラーは出ない。

どう直したか。区切りを待たずに書ける量しか端末に渡させない（背圧）。
SSHConnection.has_pending_sends は、受信ウィンドウの空きが区切り 1 つ分
（512 文字を UTF-8 にした最大 2,048 バイト）に満たない・鍵交換中・TCP へ
書けない、のどれかなら True を返し、端末は次の区切りを渡さずに待つ。
見張り役（core.send_backpressure.DrainWatcher）が空きを見張り、書けるように
なったら send_drained を出して端末に続きを渡させる。書き込みは今までどおり
send_command の sendall で、空きがあるので待たずに終わり、GUI も止まらない。
未送信の分は端末の列に残るので、マクロの停止などで取り消せる。
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

LINE = ("interface GigabitEthernet0/1\n"
        " description uplink to core.example.com\n"
        " no shutdown\n")


def _paste_body(size):
    """size バイトほどの設定の貼り付け（改行は LF）"""
    return LINE * (size // len(LINE))


def _wire_bytes(body):
    """端末が機器へ送るバイト列（改行は CR）"""
    return body.replace("\n", "\r").encode("utf-8")


class _ShellServer(paramiko.ServerInterface):
    """パスワード認証を通し、pty とシェルを受け付ける"""

    def __init__(self):
        self.shell = threading.Event()

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_pty_request(self, channel, term, width, height,
                                  pixelwidth, pixelheight, modes):
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True

    def check_channel_window_change_request(self, channel, width, height,
                                            pixelwidth, pixelheight):
        return True


class SlowSSHServer:
    """読むのが遅い SSH サーバ（localhost）。

    受信ウィンドウを window に絞り、burst バイトまで読んだら pause 秒休む。
    reading を clear すると読むのをやめる（機器が詰まった状態）。
    """

    def __init__(self, window=16384, burst=16384, pause=0.25):
        self.key = paramiko.ECDSAKey.generate()
        self.window = window
        self.burst = burst
        self.pause = pause
        self.received = bytearray()
        self.reading = threading.Event()
        self.reading.set()
        self.stop = threading.Event()
        self.channel = None
        self.transport = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            accepted, _ = self.sock.accept()
        except OSError:
            return
        transport = paramiko.Transport(accepted)
        self.transport = transport
        transport.default_window_size = self.window
        transport.add_server_key(self.key)
        server = _ShellServer()
        try:
            transport.start_server(server=server)
        except Exception:
            return
        channel = transport.accept(10)
        if channel is None or not server.shell.wait(10):
            transport.close()
            return
        self.channel = channel
        channel.settimeout(0.2)
        while not self.stop.is_set():
            if not self.reading.is_set():
                self.reading.wait(0.05)
                continue
            try:
                data = channel.recv(self.burst)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data:
                break
            self.received.extend(data)
            if self.pause:
                time.sleep(self.pause)

    def close(self):
        self.stop.set()
        self.reading.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self.transport is not None:
            self.transport.close()


class SSHPasteToSlowReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshslow-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=d)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self, **server_kwargs):
        """遅い相手へ繋いだ SSHConnection と、それへ送る端末を返す

        配線は MainWindow と同じ（接続が背圧を持っていれば端末へ渡す）。
        """
        from core.ssh_connection import SSHConnection
        from ui.terminal_widget import TerminalWidget
        server = SlowSSHServer(**server_kwargs)
        self.addCleanup(server.close)
        conn = SSHConnection("127.0.0.1", server.port, "u", "p")
        self.addCleanup(conn.dispose)
        self.errors = []
        self.closed = []
        conn.error_occurred.connect(self.errors.append)
        conn.disconnected.connect(lambda: self.closed.append(True))
        self.assertTrue(conn.connect(), "前提: localhost の SSH に繋がる")
        deadline = time.time() + 5
        while server.channel is None and time.time() < deadline:
            time.sleep(0.01)

        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        term.key_pressed.connect(conn.send_command)
        backlog = getattr(conn, "has_pending_sends", None)
        drained = getattr(conn, "send_drained", None)
        if backlog is not None and drained is not None:
            term.set_send_backlog(backlog)
            drained.connect(term.resume_send_queue)
        return server, conn, widget, term

    def _pump_until(self, done, seconds):
        end = time.time() + seconds
        while time.time() < end and not done():
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _ticker(self):
        """GUI の短い周期のタイマー。鳴った時刻を控える"""
        from PyQt6.QtCore import QTimer
        stamps = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: stamps.append(time.perf_counter()))
        timer.start()
        self.addCleanup(timer.stop)
        return stamps

    def test_a_large_paste_reaches_a_slow_reader_whole_and_in_order(self):
        server, conn, _, term = self._session()
        body = _paste_body(96 * 1024)
        expected = _wire_bytes(body)
        stamps = self._ticker()

        term.send_text(body)
        self._pump_until(
            lambda: len(server.received) >= len(expected) or self.errors
            or self.closed, 60)

        self.assertEqual([], self.errors, "送信が切断として扱われた")
        self.assertEqual([], self.closed, "セッションが切れた")
        self.assertTrue(conn.is_connected)
        self.assertEqual(len(expected), len(server.received),
                         "相手に届いたバイト数が違う")
        self.assertEqual(expected, bytes(server.received),
                         "届いた順序や中身が違う")
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertGreater(len(stamps), 20, "送信中に GUI のタイマーが進んでいない")
        self.assertLess(max(gaps), 1.0,
                        "送信中に GUI が %.2f 秒止まった" % max(gaps))

    def test_a_stalled_reader_keeps_the_session_and_the_rest_stays_queued(self):
        """相手が読まなくなっても切らず、渡していない分は端末の列に残ること。"""
        server, conn, _, term = self._session()
        server.reading.clear()
        body = _paste_body(64 * 1024)

        term.send_text(body)
        self._pump_until(lambda: self.errors or self.closed, 0.8)

        self.assertEqual([], self.errors, "相手が詰まっただけで送信エラーになった")
        self.assertEqual([], self.closed)
        self.assertTrue(conn.is_connected)
        self.assertTrue(term._send_queue,
                        "渡していない分が端末の列に残っていない（取り消せない）")

        # 詰まったままでも、後始末で見張りが止まり、固まらない
        watcher = conn._drain_watcher
        thread = watcher._thread if watcher is not None else None
        started = time.perf_counter()
        conn.dispose()
        self.assertLess(time.perf_counter() - started, 2.5, "後始末が固まった")
        if thread is not None:
            thread.join(1.0)
            self.assertFalse(thread.is_alive(), "見張りのスレッドが残った")
        self._pump_until(lambda: False, 0.2)
        self.assertEqual([], self.errors, "後始末のあとにエラーが出た")

    def test_typed_keys_are_written_at_once(self):
        """打鍵はその場で書き、区切り 1 つ分の待ちを足さないこと。"""
        server, conn, _, term = self._session(pause=0)

        term.send_text("show clock")

        self.assertEqual([], term._send_queue, "打鍵が端末の列に残った")
        self.assertFalse(term._sending, "打鍵のあと送信待ちのまま")
        self._pump_until(lambda: len(server.received) >= 10, 5)
        self.assertEqual(b"show clock", bytes(server.received))

    def test_a_peer_that_closes_while_backlogged_is_reported(self):
        """待っている間に相手が閉じたら、切断として知らせて見張りも終わること。"""
        server, conn, _, term = self._session()
        server.reading.clear()
        term.send_text(_paste_body(64 * 1024))
        self._pump_until(lambda: self.errors or self.closed, 0.5)
        watcher = conn._drain_watcher
        thread = watcher._thread if watcher is not None else None

        server.channel.close()
        self._pump_until(
            lambda: self.closed or any("送信エラー" in e for e in self.errors), 5)

        self.assertTrue(
            self.closed or any("送信エラー" in e for e in self.errors),
            "相手が閉じたのに知らせが無い: %r" % (self.errors,))
        if thread is not None:
            thread.join(2.0)
            self.assertFalse(thread.is_alive(), "見張りのスレッドが残った")


class DrainWatcherTest(unittest.TestCase):
    """見張り役そのものの約束。"""

    def test_nothing_is_started_while_there_is_room(self):
        from core.send_backpressure import DrainWatcher
        notified = []
        watcher = DrainWatcher(lambda: False, lambda: notified.append(1))

        self.assertFalse(watcher.check())
        self.assertIsNone(watcher._thread)
        self.assertEqual([], notified)

    def test_one_watcher_notifies_once_when_room_comes_back(self):
        from core.send_backpressure import DrainWatcher
        busy = threading.Event()
        busy.set()
        notified = []
        watcher = DrainWatcher(busy.is_set, lambda: notified.append(1))

        self.assertTrue(watcher.check())
        first = watcher._thread
        self.assertTrue(watcher.check())
        self.assertIs(first, watcher._thread, "見張りが 2 つ動いた")
        busy.clear()
        first.join(2.0)

        self.assertFalse(first.is_alive())
        self.assertEqual([1], notified)
        self.assertIsNone(watcher._thread)

    def test_stop_ends_the_watch_without_notifying(self):
        from core.send_backpressure import DrainWatcher
        notified = []
        watcher = DrainWatcher(lambda: True, lambda: notified.append(1))
        self.assertTrue(watcher.check())
        thread = watcher._thread

        watcher.stop()

        self.assertFalse(thread.is_alive(), "止めても見張りが残った")
        self.assertEqual([], notified)
        self.assertFalse(watcher.check(), "止めた後も待たせている")

    def test_the_room_covers_one_terminal_chunk(self):
        """端末の区切り（文字数）を UTF-8 にした最大のバイト数が空きの基準に収まること。"""
        from core.send_backpressure import MIN_SEND_ROOM
        from ui.terminal_widget import InteractiveTerminal
        self.assertGreaterEqual(MIN_SEND_ROOM, InteractiveTerminal.SEND_CHUNK * 4)


if __name__ == "__main__":
    unittest.main()
