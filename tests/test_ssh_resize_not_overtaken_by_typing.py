"""保留した端末の大きさ（window-change）を、そのあとの打鍵が追い越さないことを検証する。

何が起きていたか（78ef376 で実測、localhost の paramiko サーバ。サーバは
チャネルを読まず、window-change を受けた時点でそのチャネルに届いていた
データのバイト数を控える）。1.3.2 では、鍵交換中・TCP へ書けない間は
window-change を送らずに大きさだけを覚え（_size_unsent）、専用の見張りが
Transport へ書けるようになったと知らせた（_size_writable。Qt のキュー接続）
ところで送る。ところがデータの送信（_write_carry）は「いま待たずに書けるか」
（_send_backlogged: 鍵交換中でない・窓が空いている・TCP へ書ける・書き手が
空いている）しか見ず、保留した大きさを確かめていなかった。
  - clear_to_send.clear() → set_terminal_size(132, 43) → clear_to_send.set()
    → Qt のイベントを処理せず send_command('x'): 10 回とも、機器は 'x'
    （1 バイト）を受けたあとに window-change を受けた
  - 詰まっている間に打った 'x'（持ち越しに残る）: 詰まりが解けると、データの
    見張り（send_drained）と大きさの見張り（_size_writable）のどちらが先に
    知らせるかの競争になり、20 回中 14 回は 'x' が先に届いた
機器は古い幅・高さのまま、その打鍵を処理しうる（全画面の表示やページングが
画面と食い違う）。441ea02 は resize_pty を GUI スレッドでその場で書き、書き
終えてから次の打鍵を送っていた（同じ localhost で、鍵交換を 0.3 秒後に
終わらせる構成: 10 回とも window-change が先）。

どう直したか。保留した大きさがある間は、データも「待たずには書けない」
（_send_backlogged が True）として扱う。打鍵は持ち越し（_carry）と端末の
列に残り、大きさの見張りが大きさを書き手へ渡したあとで、データの見張りが
send_drained を出して続きを書く。書き手は渡された順に書くので、window-change
がそのあとの打鍵より先に届く。大きさの見張りはこれまでどおり Transport だけを
見るので、窓が 0 のままでも詰まりが解ければ大きさは 1 回送られる。GUI
スレッドは待たない（どちらの見張りも別のスレッドで状態を読むだけ）。
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


class _OrderRecordingSSHServer:
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


class SizeBeforeLaterTypingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshresizeorder-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=d)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _session(self):
        from core.ssh_connection import SSHConnection
        server = _OrderRecordingSSHServer()
        self.addCleanup(server.close)
        conn = SSHConnection("127.0.0.1", server.port, "u", "p")
        self.addCleanup(conn.dispose)
        self.errors, self.closed = [], []
        conn.error_occurred.connect(self.errors.append)
        conn.disconnected.connect(lambda: self.closed.append(True))
        self.assertTrue(conn.connect(), "前提: localhost の SSH に繋がる")
        self.assertTrue(server.ready.wait(5))
        self._pump(0.2)
        transport = conn.client.get_transport()
        # 鍵交換が終わらないまま止まっても、テストの後始末で必ず戻す
        self.addCleanup(transport.clear_to_send.set)
        return conn, server, transport

    def _wait_for_both(self, server, data_bytes):
        self._pump(5, until=lambda: (server.resizes
                                     and server.received() >= data_bytes)
                   or self.errors or self.closed)
        self._pump(0.2)
        self.assertEqual([], self.errors)
        self.assertEqual([], self.closed)

    def test_a_key_right_after_the_transport_recovers_follows_the_size(self):
        """Transport が戻った直後、大きさの知らせを処理する前に来た打鍵"""
        conn, server, transport = self._session()
        transport.clear_to_send.clear()          # 鍵交換中
        conn.set_terminal_size(132, 43)
        self.assertEqual([], server.resizes, "前提: 鍵交換中に window-change を書いた")
        self.assertTrue(conn._size_unsent, "前提: 大きさが保留されていない")

        transport.clear_to_send.set()            # 鍵交換が終わる
        conn.send_command("x")                   # Qt のイベントを処理する前の打鍵
        self._wait_for_both(server, 1)

        self.assertEqual(1, server.received(), "打鍵が機器へ届いていない")
        self.assertEqual([(132, 43, 0)], server.resizes,
                         "保留していた window-change を、あとの打鍵が追い越した"
                         "（3 つ目は window-change より先に届いたバイト数）")

    def test_a_key_held_while_stuck_follows_the_size_when_drained_first(self):
        """詰まっている間に打った分を、データの見張りの知らせが先に書き出す場合

        詰まりが解けたとき、データの見張り（send_drained）と大きさの見張り
        （_size_writable）のどちらが先に知らせるかは競争になる。ここでは
        データの見張りが先に知らせたのと同じに、send_drained を先に出す。
        """
        conn, server, transport = self._session()
        transport.clear_to_send.clear()
        conn.set_terminal_size(132, 43)
        conn.send_command("x")                   # 詰まっている間の打鍵（持ち越し）
        self.assertEqual(0, server.received(), "前提: 鍵交換中にデータを書いた")

        transport.clear_to_send.set()
        conn.send_drained.emit()                 # データの見張りが先に知らせた
        self._wait_for_both(server, 1)

        self.assertEqual(1, server.received(), "打鍵が機器へ届いていない")
        self.assertEqual([(132, 43, 0)], server.resizes,
                         "詰まっている間に保留した window-change を、同じ間に"
                         "打った（あとから来た）データが追い越した")

    def test_the_terminal_holds_typing_until_the_size_is_out(self):
        """端末の送信の列を通しても、大きさが先に届き、打鍵はそのあと止まらずに届く"""
        from ui.terminal_widget import TerminalWidget
        conn, server, transport = self._session()
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        term.key_pressed.connect(conn.send_command)
        term.set_send_backlog(conn.has_pending_sends)
        conn.send_drained.connect(term.resume_send_queue)

        transport.clear_to_send.clear()
        conn.set_terminal_size(132, 43)
        transport.clear_to_send.set()
        self.assertTrue(term.send_text("show version\n"))   # 次のイベントより前
        self._wait_for_both(server, len(b"show version\r"))

        self.assertEqual(len(b"show version\r"), server.received(),
                         "打鍵が全部届いていない（端末の列に残ったまま）")
        self.assertEqual(b"show version\r",
                         server.channel.in_buffer.read(64, 1))
        self.assertEqual([(132, 43, 0)], server.resizes,
                         "端末から渡した打鍵が、保留していた window-change を追い越した")
        self._pump(0.3)
        self.assertFalse(conn.has_pending_sends(),
                         "大きさを送り終えたのに、端末に次を渡させない")


if __name__ == "__main__":
    unittest.main()
