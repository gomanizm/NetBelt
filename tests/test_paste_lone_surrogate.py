"""送れない文字（孤立したサロゲート）を含む貼り付けを、送る前に丸ごと断ることを検証する（SSH・Telnet）。

何が起きていたか（基準 441ea02 で実測、MainWindow と localhost の相手）。
貼り付けは 'a'×600, CR, 'b'×10, U+D800, 'b'×10, CR, 'c'×1100, CR。端末は
512 文字ずつの区切りで渡すので、U+D800 は 2 つ目の区切りに入る。
  - SSH も Telnet も、相手が受け取ったのは 1 つ目の区切りの 512 バイトだけ
    だった（'a'×512。行の途中まで）。
  - 2 つ目の区切りで send_command の encode('utf-8') が UnicodeEncodeError に
    なり、error_occurred『送信エラー: 'utf-8' codec can't encode character
    …』が出た。MainWindow は『送信エラー』を切断として扱うので、接続が
    捨てられ、端末に『セッションが切断されました』と『Enterキーを押すと
    再接続します』が出た。残りの区切りは再接続待ちで捨てられた。
孤立したサロゲートは PyQt の変換（QLineEdit の往復、QMimeData.text、
UTF-16LE の QStringDecoder）を通っても残るので、壊れた UTF-16 をクリップ
ボードへ置くアプリからの貼り付けで起きうる。

どう直したか（利用者の決定 (a)）。
  - 貼り付けの入口 InteractiveTerminal.send_text で、UTF-8 にできない文字を
    含むなら区切りに分ける前に丸ごと断り、何も送らない。端末に理由
    （どの文字が何文字目にあるか）を出す。
  - 保険として、SSH と Telnet の send_command は、符号化できない区切りを
    切断として扱わない。何も送らずに、端末へ理由を出す。
"""
import os
import socket
import sys
import tempfile
import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402

SUR = chr(0xD800)
TEXT = "a" * 600 + "\r" + "b" * 10 + SUR + "b" * 10 + "\r" + "c" * 1100 + "\r"
NOTICE = "送れない文字"


class _App:
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()


class TerminalRefusesUnsendablePasteTest(_App, unittest.TestCase):
    """端末の入口で、区切りに分ける前に丸ごと断る"""

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        handed = []
        term.key_pressed.connect(handed.append)
        return term, handed

    def test_nothing_is_handed_to_the_connection(self):
        term, handed = self._terminal()

        sent = term.send_text(TEXT)
        self._pump(0.3)

        self.assertEqual([], handed,
                         "送れない文字より前の区切りを接続へ渡した（途中まで送られる）")
        self.assertFalse(sent, "送らなかったのに送ったと返した")
        self.assertEqual([], term._send_queue, "断った貼り付けが送信列に残った")

    def test_the_user_is_told_why(self):
        term, _ = self._terminal()

        term.send_text(TEXT)
        self._pump(0.3)

        shown = term.toPlainText()
        self.assertIn(NOTICE, shown, "断ったことが端末に出ていない")
        self.assertIn("U+D800", shown, "どの文字かが分からない")
        self.assertIn("612 文字目", shown, "どこにあるかが分からない")

    def test_a_normal_paste_is_unchanged(self):
        term, handed = self._terminal()
        text = "a" * 600 + "\r" + "b" * 10 + "\r"

        self.assertTrue(term.send_text(text))
        self._pump(0.3)

        self.assertEqual(text, "".join(handed))
        self.assertNotIn(NOTICE, term.toPlainText())


class ConnectionSafetyNetTest(_App, unittest.TestCase):
    """端末の入口を通らずに届いた区切りも、切断として扱わない"""

    def test_ssh_refuses_without_dropping_the_session(self):
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.channel = mock.Mock()
        conn.is_connected = True
        errors, shown, closed = [], [], []
        conn.error_occurred.connect(errors.append)
        conn.output_received.connect(shown.append)
        conn.disconnected.connect(lambda: closed.append(True))

        conn.send_command("ab" + SUR)

        self.assertEqual([], errors, "送れない文字が送信エラー（切断）になった")
        self.assertEqual([], closed)
        self.assertTrue(conn.is_connected)
        conn.channel.sendall.assert_not_called()
        self.assertEqual(b"", conn._carry, "断った区切りが書き残しに残った")
        self.assertTrue(any(NOTICE in text for text in shown),
                        "断ったことが知らされていない: %r" % (shown,))

        conn.send_command("x")
        conn.channel.sendall.assert_called_once_with(b"x")

    def test_telnet_refuses_without_dropping_the_session(self):
        from core.telnet_connection import TelnetConnection
        conn = TelnetConnection("192.0.2.1", 23)
        conn.socket = mock.Mock()
        conn.is_connected = True
        errors, shown, closed = [], [], []
        conn.error_occurred.connect(errors.append)
        conn.output_received.connect(shown.append)
        conn.disconnected.connect(lambda: closed.append(True))

        conn.send_command("ab" + SUR)

        self.assertEqual([], errors, "送れない文字が送信エラー（切断）になった")
        self.assertEqual([], closed)
        self.assertTrue(conn.is_connected)
        conn.socket.sendall.assert_not_called()
        conn.socket.send.assert_not_called()
        self.assertEqual(b"", bytes(conn._out_data))
        self.assertTrue(any(NOTICE in text for text in shown),
                        "断ったことが知らされていない: %r" % (shown,))

        conn.send_command("x")
        conn.socket.sendall.assert_called_once_with(b"x")


class _Shell(paramiko.ServerInterface):
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


class _Peer:
    """1 接続だけ受ける localhost の相手（SSH のシェル・Telnet）。受信を溜める"""

    def __init__(self, ssh):
        self.ssh = ssh
        self.received = bytearray()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.transport = None
        self.conn = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            accepted, _ = self.sock.accept()
        except OSError:
            return
        if self.ssh:
            t = paramiko.Transport(accepted)
            self.transport = t
            t.add_server_key(paramiko.ECDSAKey.generate())
            iface = _Shell()
            try:
                t.start_server(server=iface)
            except Exception:
                return
            ch = t.accept(10)
            if ch is None or not iface.shell.wait(10):
                return
            reader = ch
        else:
            self.conn = accepted
            reader = accepted
        reader.settimeout(0.2)
        reader.sendall(b"sw1# ")
        while not self.stop.is_set():
            try:
                d = reader.recv(65536)
            except socket.timeout:
                continue
            except Exception:
                break
            if not d:
                break
            self.received.extend(d)

    def close(self):
        self.stop.set()
        for s in (self.sock, self.conn):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
        if self.transport is not None:
            self.transport.close()


class MainWindowPasteTest(_App, unittest.TestCase):
    """MainWindow の配線で、SSH と Telnet の機器へ何も届かず、接続も保たれる"""

    def _window(self, protocol):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = Path(tempfile.mkdtemp(prefix="netbelt-surrogate-"))
        peer = _Peer(ssh=protocol == "ssh")
        self.addCleanup(peer.close)
        device = {"name": "sw1", "host": "127.0.0.1", "port": peer.port,
                  "protocol": protocol, "username": "admin", "password": "pw"}
        config_path = d / "config.json"
        config_path.write_text(json.dumps({
            "groups": [{"name": "Default", "devices": [device]}],
            "global_macros": []}), encoding="utf-8")
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(config_path))),
                mock.patch("ui.main_window.MainWindow._start_sftp_session"),
                mock.patch("ui.main_window.QMessageBox")):
            patcher.start()
            self.addCleanup(patcher.stop)
        window = MainWindow()
        self.addCleanup(self._close, window)
        window._on_connect_requested(
            window.config_manager.get_groups()[0]["devices"][0])
        self._pump(15, until=lambda: (
            window.connections.get("sw1") is not None
            and window.connections["sw1"].is_connected
            and window.terminal_widget._terminals["sw1"].can_send_input()
            and peer.received is not None))
        self._pump(0.5)
        return window, peer

    @staticmethod
    def _close(window):
        for name in list(window.connections):
            window._dispose_connection(name)
        with mock.patch("sys.excepthook", lambda *args: None):
            window.close()

    def _check(self, protocol):
        window, peer = self._window(protocol)
        conn = window.connections.get("sw1")
        self.assertIsNotNone(conn, "前提: 繋がっていない")
        term = window.terminal_widget._terminals["sw1"]
        before = len(peer.received)

        term.send_text(TEXT)
        self._pump(2)

        self.assertEqual(b"", bytes(peer.received[before:]),
                         "送れない文字より前の区切りが機器へ届いた")
        self.assertIs(conn, window.connections.get("sw1"),
                      "接続が捨てられた（切断として扱われた）")
        self.assertTrue(conn.is_connected)
        self.assertFalse(term._reconnect_mode, "再接続待ちに入った")
        shown = term.toPlainText()
        self.assertIn(NOTICE, shown, "断ったことが端末に出ていない")
        self.assertNotIn("セッションが切断されました", shown)

        # 断ったあとも、普通の入力はそのまま届く
        term.send_text("show clock\r")
        self._pump(3, until=lambda: b"show clock\r" in bytes(peer.received))
        self.assertIn(b"show clock\r", bytes(peer.received[before:]))

    def test_ssh(self):
        self._check("ssh")

    def test_telnet(self):
        self._check("telnet")


if __name__ == "__main__":
    unittest.main()
