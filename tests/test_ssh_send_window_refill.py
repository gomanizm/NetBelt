"""SSH の送信が、受信ウィンドウの補充を遅らせる機器で止まらないことを検証する。

何が起きていたか（ae5c3d5 の背圧で実測。相手は localhost の paramiko サーバで遅延なし）。
SSHConnection.has_pending_sends は、受信ウィンドウの空きが
min(2,048 バイト, これまでの最大の窓) を下回ると True を返し、端末は次の
区切りを渡さずに待っていた。ところが補充（WINDOW_ADJUST）をいつ送るかは
相手が決める。相手が「空きがそこまで減る前には補充しない」作りだと、
こちらは送るのをやめ、送らないので相手の消費も進まず、補充は永遠に来ない。
貼り付けも打鍵もマクロも 1 バイトも届かなくなり、エラーも切断も出なかった。
  - ウィンドウ 2,048・補充は既定（1/10 読むごと）: 1 文字ずつ 100 打鍵して、届いたのは 1 バイト
  - ウィンドウ 1,024・補充は既定: 3,000 打鍵して、届いたのは 1 バイト
  - ウィンドウ 8,192・7,000 バイト読むごとに補充: 16KB の貼り付けが 6,656 バイトで止まった
また、ウィンドウが区切り 1 つのバイト数（512 文字 × UTF-8 の最大 4 バイト）より
小さいと、全角の貼り付けで sendall がウィンドウ待ちの時間切れ（0.1 秒）になり、
『送信エラー: 』で切断した（ウィンドウ 1,535 で 7,200 バイトのうち 3,071 バイト）。

どう直したか。待つのは、送れるウィンドウが 0 のとき・鍵交換中・TCP へ
書けないときだけにした。1 バイトでも空いていれば、入る分だけを待たずに
書く（OpenSSH・PuTTY・Tera Term と同じ。ウィンドウが 0 なら、相手は消費した
あと必ず補充する）。入り切らなかった残り（区切り 1 つ分まで）は接続が
持ち越し、後から来た送信はその後ろへ足すので順序は崩れない。持ち越しは、
端末が次を渡す前（has_pending_sends）と、見張りが書けるようになったと
知らせたときに、GUI スレッドで書き出す。切断・後始末では捨てる。
"""
import os
import shutil
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
# 全角（UTF-8 で 3 バイト）。端末の区切り 512 文字がちょうど 1,536 バイトになる
WIDE = chr(0x3007)


class _ShellServer(paramiko.ServerInterface):
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


class RefillSSHServer:
    """受信ウィンドウの補充を自分の都合で送る SSH サーバ（localhost）。

    window: 広告する受信ウィンドウ。threshold: 何バイト読んだら補充するか
    （None なら paramiko の既定 = window // 10）。read_size / pause: 1 回に
    読む量と、読んだあとの休み。hold で読むのをやめ、reading を set すると
    また読む。接続ごとに受け取った分を sessions に控える。
    """

    def __init__(self, window, threshold=None, read_size=4096, pause=0.0):
        self.key = paramiko.ECDSAKey.generate()
        self.window = window
        self.threshold = threshold
        self.read_size = read_size
        self.pause = pause
        self.sessions = []
        self.transports = []
        self.reading = threading.Event()
        self.reading.set()
        # 読むのをやめて待っている（hold が確かめる）
        self.parked = threading.Event()
        self.stop = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def received(self):
        return self.sessions[0] if self.sessions else bytearray()

    def hold(self):
        """読むのをやめさせ、読み取りの途中（recv の待ち）を抜けて止まるまで待つ"""
        self.parked.clear()
        self.reading.clear()
        if not self.parked.wait(2.0):
            raise AssertionError("前提: 相手が読むのをやめない")

    def _accept(self):
        while not self.stop.is_set():
            try:
                accepted, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(accepted,),
                             daemon=True).start()

    def _serve(self, accepted):
        transport = paramiko.Transport(accepted)
        self.transports.append(transport)
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
        if self.threshold is not None:
            channel.in_window_threshold = self.threshold
        received = bytearray()
        self.sessions.append(received)
        channel.settimeout(0.2)
        while not self.stop.is_set():
            if not self.reading.is_set():
                self.parked.set()
                self.reading.wait(0.05)
                continue
            try:
                data = channel.recv(self.read_size)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data:
                break
            received.extend(data)
            if self.pause:
                time.sleep(self.pause)

    def close(self):
        self.stop.set()
        self.reading.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for transport in self.transports:
            transport.close()


def _pump(app, seconds, until=None):
    end = time.time() + seconds
    while time.time() < end and not (until and until()):
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()


class SSHSendThroughMainWindowTest(unittest.TestCase):
    """MainWindow の打鍵・右クリックの貼り付けで、補充の遅い相手へ送る。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshrefill-"))
        self.addCleanup(shutil.rmtree, d, True)
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(d / "config.json"))),
                # SFTP はこの検証に関係ない（相手のサーバも答えない）
                mock.patch.object(MainWindow, "_start_sftp_session")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = MainWindow()
        self.addCleanup(self._close_window)

    def _close_window(self):
        self.window.macro_manager.cleanup_device("dev")
        self.window.close()
        _pump(self.app, 0.1)

    def _connect(self, server):
        self.addCleanup(server.close)
        device = {"name": "dev", "host": "127.0.0.1", "port": server.port,
                  "protocol": "ssh", "username": "u", "password": "p"}
        self.window._connect_ssh(device)
        _pump(self.app, 10, until=lambda: self._terminal().can_send_input()
              and server.sessions)
        self.assertTrue(self._terminal().can_send_input(), "前提: 接続できている")
        return self.window.connections["dev"]

    def _terminal(self):
        return self.window.terminal_widget._terminals["dev"]

    def _still_connected(self, conn):
        return (self.window.connections.get("dev") is conn
                and not self._terminal()._reconnect_mode)

    def _type(self, count):
        """1 文字ずつ打鍵する（60 文字ごとに Enter）。機器へ届くはずのバイト列を返す"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        term = self._terminal()
        for i in range(count):
            if i % 60 == 59:
                QTest.keyClick(term, Qt.Key.Key_Return)
            else:
                QTest.keyClick(term, Qt.Key.Key_A)
            self.app.processEvents()
        return (b"a" * 59 + b"\r") * (count // 60) + b"a" * (count % 60)

    def _paste(self, text):
        """右クリックの貼り付け（確認ダイアログが出れば送信を選ぶ）"""
        from PyQt6.QtWidgets import QApplication, QDialog
        import ui.dialogs.paste_confirm_dialog as confirm
        QApplication.clipboard().setText(text)
        with mock.patch.object(confirm.PasteConfirmDialog, "exec",
                               return_value=QDialog.DialogCode.Accepted):
            self._terminal().custom_paste()

    def _wait_for(self, conn, received, expected, seconds):
        _pump(self.app, seconds, until=lambda: len(received) >= len(expected)
              or not self._still_connected(conn))
        _pump(self.app, 0.2)

    def _assert_arrived_whole(self, conn, received, expected):
        self.assertTrue(self._still_connected(conn),
                        "セッションが切れた: %s"
                        % self.window.status_bar.currentMessage())
        self.assertEqual(len(expected), len(received),
                         "相手に届いたバイト数が違う（送信が止まった）")
        self.assertEqual(expected, bytes(received), "届いた順序や中身が違う")

    def test_keystrokes_reach_a_peer_that_refills_a_2048_window_late(self):
        server = RefillSSHServer(window=2048)
        conn = self._connect(server)

        expected = self._type(100)
        self._wait_for(conn, server.received, expected, 10)

        self._assert_arrived_whole(conn, server.received, expected)

    def test_keystrokes_reach_a_peer_with_a_1024_window(self):
        server = RefillSSHServer(window=1024)
        conn = self._connect(server)

        expected = self._type(3000)
        self._wait_for(conn, server.received, expected, 15)

        self._assert_arrived_whole(conn, server.received, expected)

    def test_a_paste_reaches_a_peer_that_refills_after_7000_bytes(self):
        server = RefillSSHServer(window=8192, threshold=7000)
        conn = self._connect(server)
        body = LINE * (16 * 1024 // len(LINE))
        expected = body.replace("\n", "\r").encode("utf-8")

        self._paste(body)
        self._wait_for(conn, server.received, expected, 15)

        self._assert_arrived_whole(conn, server.received, expected)

    def test_a_wide_paste_fits_a_window_smaller_than_one_chunk(self):
        """区切り 1 つ（1,536 バイト）より小さいウィンドウでも切れずに全部届くこと。"""
        server = RefillSSHServer(window=1535, read_size=1024, pause=0.15)
        conn = self._connect(server)
        body = WIDE * 2400
        expected = body.encode("utf-8")

        self._paste(body)
        self._wait_for(conn, server.received, expected, 30)

        self._assert_arrived_whole(conn, server.received, expected)

    def test_the_last_bytes_of_a_paste_follow_once_the_peer_reads(self):
        """最後の区切りが入り切らず、端末に渡す物が無くなっても、残りが後から届くこと。"""
        server = RefillSSHServer(window=1535)
        conn = self._connect(server)
        server.hold()
        body = WIDE * 512
        expected = body.encode("utf-8")

        self._paste(body)
        _pump(self.app, 0.5, until=lambda: not self._still_connected(conn))
        self.assertTrue(self._still_connected(conn),
                        "相手が読まないだけでセッションが切れた: %s"
                        % self.window.status_bar.currentMessage())
        self.assertEqual([], self._terminal()._send_queue,
                         "前提: 端末には渡す物が残っていない")

        server.reading.set()
        self._wait_for(conn, server.received, expected, 10)

        self._assert_arrived_whole(conn, server.received, expected)

    def test_a_large_paste_to_a_fast_peer_arrives_whole(self):
        """速い相手（OpenSSH 相当の 2MB ウィンドウ）へ 500KB を送り切ること。"""
        server = RefillSSHServer(window=2 * 1024 * 1024, read_size=32768)
        conn = self._connect(server)
        body = LINE * (500 * 1024 // len(LINE))
        expected = body.replace("\n", "\r").encode("utf-8")

        self._paste(body)
        self._wait_for(conn, server.received, expected, 60)

        self._assert_arrived_whole(conn, server.received, expected)


class SSHCarryTest(unittest.TestCase):
    """入り切らなかった残り（持ち越し）の扱い。接続を直接使う。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshcarry-"))
        self.addCleanup(shutil.rmtree, d, True)
        patcher = mock.patch("core.config_manager.app_data_dir", return_value=d)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connect(self, server):
        from core.ssh_connection import SSHConnection
        self.addCleanup(server.close)
        conn = SSHConnection("127.0.0.1", server.port, "u", "p")
        self.addCleanup(conn.dispose)
        self.errors = []
        conn.error_occurred.connect(self.errors.append)
        self.assertTrue(conn.connect(), "前提: localhost の SSH に繋がる")
        _pump(self.app, 5, until=lambda: server.sessions)
        return conn

    def test_a_later_send_does_not_overtake_the_carried_rest(self):
        server = RefillSSHServer(window=1535)
        conn = self._connect(server)
        server.hold()
        chunk = (WIDE * 512).encode("utf-8")

        conn.send_command(WIDE * 512)
        conn.send_command("X")
        _pump(self.app, 0.3)
        self.assertEqual([], self.errors, "ウィンドウ待ちで送信エラーになった")
        server.reading.set()
        _pump(self.app, 5, until=lambda: len(server.received) >= len(chunk) + 1)
        _pump(self.app, 0.2)

        self.assertEqual([], self.errors)
        self.assertEqual(chunk + b"X", bytes(server.received),
                         "後から送った文字が残りを追い越した")

    def test_disconnect_drops_the_carried_rest(self):
        """切断した接続の残りを、繋ぎ直した先へ書かないこと。"""
        server = RefillSSHServer(window=1535)
        conn = self._connect(server)
        server.hold()

        conn.send_command(WIDE * 512)
        _pump(self.app, 0.3)
        self.assertEqual([], self.errors, "ウィンドウ待ちで送信エラーになった")
        conn.disconnect()
        server.reading.set()
        self.assertTrue(conn.connect(), "前提: 繋ぎ直せる")
        _pump(self.app, 5, until=lambda: len(server.sessions) >= 2)
        _pump(self.app, 0.8)
        self.assertEqual(b"", bytes(server.sessions[1]),
                         "前の接続の残りが新しい接続へ流れた")

        conn.send_command("x")
        _pump(self.app, 5, until=lambda: len(server.sessions[1]) >= 1)
        _pump(self.app, 0.2)
        self.assertEqual(b"x", bytes(server.sessions[1]))


if __name__ == "__main__":
    unittest.main()
