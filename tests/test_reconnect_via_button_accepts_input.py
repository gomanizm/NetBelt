"""接続ボタン経由の再接続で、入力禁止が解けることを検証する。

切断されたタブは再接続待ち（_reconnect_mode）に入り、Enter 以外の打鍵と
貼り付けを捨てる。Enter で再接続すると keyPressEvent がこのフラグを
落とすが、接続ボタンやダブルクリックで再接続すると、同じタブを使い回す
create_terminal_tab はフラグに触れず、接続に成功しても打鍵も貼り付けも
機器へ届かなかった（実測: can_send_input=False のまま、typed 'a' -> sent []）。
Enter を押せば解けるが、そのとき「再接続中...」が画面に出て、ステータス
バーは「既に接続されています」になる。

タブを再利用して新しいセッションを始める時点で、再接続待ちを解く。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, pyqtSignal

sys.path.insert(0, "src")


class FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。connect() は即座に成功を知らせる。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []
        FakeSSH.instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        self.sent.append(command)

    def dispose(self):
        pass

    def disconnect(self):
        pass


class ReconnectViaButtonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-reconn-")))
        ap.start()
        self.addCleanup(ap.stop)
        FakeSSH.instances = []

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _window(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        return window

    def _type(self, terminal, ch):
        from PyQt6.QtCore import Qt, QEvent
        from PyQt6.QtGui import QKeyEvent
        terminal.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, ch))

    def test_reusing_the_tab_for_a_new_session_clears_the_reconnect_wait(self):
        """TerminalWidget 単体: タブの再利用で再接続待ちが解けること。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        terminal.set_input_enabled(True)
        w.enable_reconnect("dev", lambda name: None)
        self.assertFalse(terminal.can_send_input(), "前提: 切断後は入力禁止")

        w.create_terminal_tab("dev")     # 接続ボタン経由の再接続と同じ経路

        self.assertTrue(terminal.can_send_input(),
                        "タブを再利用しても再接続待ちが残っている")

    def test_button_reconnect_delivers_keystrokes_to_the_new_session(self):
        """MainWindow 通し: 接続ボタンで再接続した後の打鍵が機器へ届くこと。"""
        device = {"name": "dev", "host": "192.0.2.10", "port": 22,
                  "username": "u", "password": "", "protocol": "ssh"}
        with mock.patch("ui.main_window.SSHConnection", FakeSSH):
            window = self._window()
            window._on_connect_requested(device)
            self._pump()
            terminal = window.terminal_widget._terminals["dev"]
            self.assertTrue(terminal.can_send_input(), "前提: 初回接続後は入力できる")

            window._on_connection_closed("dev")
            self.assertFalse(terminal.can_send_input(), "前提: 切断後は入力禁止")

            with mock.patch.object(window.device_tree, "get_selected_device",
                                   return_value=("G", device)):
                window._on_connect_button_clicked()
            self._pump()

            self.assertEqual(len(FakeSSH.instances), 2, "前提: 2つ目の接続ができた")
            conn2 = FakeSSH.instances[1]
            self.assertTrue(terminal.can_send_input(),
                            "再接続に成功したのに入力が禁止されたまま")
            self._type(terminal, "a")
            self.assertEqual(conn2.sent, ["a"],
                             "打鍵が新しい接続へ届いていない")


if __name__ == "__main__":
    unittest.main()
