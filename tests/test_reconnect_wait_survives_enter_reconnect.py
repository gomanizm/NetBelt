"""Enter で始めた再接続が失敗しても、再接続待ちが残ることを検証する。

切断されたタブには「Enterキーを押すと再接続します」が出て、再接続待ち
（_reconnect_mode）に入る。keyPressEvent は reconnect_requested を出す前に
その待ちを落としており、接続に失敗しても誰も張り直さない。
_on_connection_error の一般エラー分岐（切断扱いにならないエラー）は
show_notice と後片付けだけで、切断側（_on_connection_closed）と違って
enable_reconnect を呼ばないからである。

既存の tests/test_reconnect_wait_survives_failed_reconnect.py は
「接続ボタン」経由しか踏まない。ボタンは待ちを落とさないので、この穴は
そちらでは見えない。

機器の再起動中など、再接続の失敗は普通に起きる。そのあと画面の案内どおりに
Enter を押しても何も起こらず、打鍵は破棄済みの接続へ渡って黙って消える。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, Qt, pyqtSignal

sys.path.insert(0, "src")


class FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。1 本目だけ成功し、以降は失敗する。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []
        type(self).instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        if len(type(self).instances) == 1:
            self.connected.emit()
            return True
        # SSHConnection._fail と同じ形。「送信エラー」でも
        # 「Socket is closed」でもないので一般エラー分岐へ入る
        self.error_occurred.emit("SSH接続エラー: timed out")
        return False

    def send_command(self, command):
        self.sent.append(command)

    def dispose(self):
        pass

    def disconnect(self):
        pass


class ReconnectWaitSurvivesEnterReconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-enterreconn-")))
        ap.start()
        self.addCleanup(ap.stop)
        conn = mock.patch("ui.main_window.SSHConnection", FakeSSH)
        conn.start()
        self.addCleanup(conn.stop)
        FakeSSH.instances = []
        self.device = {"name": "dev", "host": "192.0.2.10", "port": 22,
                       "username": "u", "password": "", "protocol": "ssh"}

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _type(self, terminal, key, text):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        terminal.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text))

    def _after_failed_enter_reconnect(self):
        """初回接続 → 切断 → Enter で再接続 → 失敗、まで進める。"""
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        window._on_connect_requested(self.device)
        self._pump()
        terminal = window.terminal_widget._terminals["dev"]
        self.assertTrue(terminal.can_send_input(), "前提: 初回接続後は入力できる")

        window._on_connection_closed("dev")
        self.assertFalse(terminal.can_send_input(), "前提: 切断後は入力禁止")

        self._type(terminal, Qt.Key.Key_Return, "\r")
        self._pump()
        self.assertEqual(len(FakeSSH.instances), 2,
                         "前提: Enter で 2 本目の接続が試された")
        return window, terminal

    def test_a_failed_enter_reconnect_leaves_the_tab_waiting_for_enter(self):
        """Enter 経由の再接続に失敗しても、もう一度 Enter で再接続できること。"""
        window, terminal = self._after_failed_enter_reconnect()

        self.assertFalse(terminal.can_send_input(),
                         "再接続に失敗したのに入力が通る状態になっている")
        fired = []
        terminal.reconnect_requested.connect(lambda: fired.append(1))
        self._type(terminal, Qt.Key.Key_Return, "\r")

        self.assertEqual(fired, [1],
                         "画面の案内どおりに Enter を押しても再接続が起きない")

    def test_keystrokes_are_not_handed_to_the_failed_connection(self):
        """失敗した接続へ打鍵を渡さないこと。"""
        window, terminal = self._after_failed_enter_reconnect()
        conn2 = FakeSSH.instances[1]

        self._type(terminal, Qt.Key.Key_A, "a")

        self.assertEqual(conn2.sent, [], "破棄された接続へ打鍵が渡っている")

    def test_the_notice_tells_how_to_retry(self):
        """失敗したときも、再接続のしかたを出し直すこと。

        切断時の案内は画面に残っているが、そのあとエラーが流れるので
        最後の行は「エラー: ...」になる。もう一度出しておかないと、
        まだ Enter が効くことが分からない。
        """
        window, terminal = self._after_failed_enter_reconnect()

        self.assertGreaterEqual(
            terminal.toPlainText().count("Enterキーを押すと再接続します"), 2,
            "再接続のしかたを出し直していない")


if __name__ == "__main__":
    unittest.main()
