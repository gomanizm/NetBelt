"""再接続に失敗したら、再接続待ちが残ったままであることを検証する。

切断されたタブには「Enterキーを押すと再接続します」が出て、再接続待ち
（_reconnect_mode）に入る。d4c21f0 はこの待ちをタブの再利用時点
（create_terminal_tab）で解いたため、接続ボタンで再接続して失敗すると
誰も待ちを張り直さなかった。実測: 2 本目の接続を失敗させると
can_send_input=True / _reconnect_mode=False になり、画面には案内が
残っているのに Enter は再接続を起こさず、打鍵は破棄済みの接続へ渡って
黙って消えた。

待ちを解くのは接続に成功した時点（_on_connection_success）にする。
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
    """接続の見た目だけを持つ偽物。connect() の成否は succeed で決める。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []
    # True なら毎回成功、False なら 1 本目だけ成功して以降は失敗
    always_succeed = False

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []
        type(self).instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        if type(self).always_succeed or len(type(self).instances) == 1:
            self.connected.emit()
            return True
        # SSHConnection._fail と同じ形: error_occurred を出して False を返す
        self.error_occurred.emit("SSH接続エラー: timed out")
        return False

    def send_command(self, command):
        self.sent.append(command)

    def dispose(self):
        pass

    def disconnect(self):
        pass


class ReconnectWaitSurvivesFailedReconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-reconnfail-")))
        ap.start()
        self.addCleanup(ap.stop)
        conn = mock.patch("ui.main_window.SSHConnection", FakeSSH)
        conn.start()
        self.addCleanup(conn.stop)
        FakeSSH.instances = []
        FakeSSH.always_succeed = False
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

    def _connected_then_dropped(self):
        """初回接続 → 切断（再接続待ち）まで進めた MainWindow を返す。"""
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        window._on_connect_requested(self.device)
        self._pump()
        terminal = window.terminal_widget._terminals["dev"]
        self.assertTrue(terminal.can_send_input(), "前提: 初回接続後は入力できる")

        window._on_connection_closed("dev")
        self.assertFalse(terminal.can_send_input(), "前提: 切断後は入力禁止")
        return window, terminal

    def _press_connect_button(self, window):
        with mock.patch.object(window.device_tree, "get_selected_device",
                               return_value=("G", self.device)):
            window._on_connect_button_clicked()
        self._pump()

    def _after_failed_reconnect(self):
        window, terminal = self._connected_then_dropped()
        self._press_connect_button(window)
        self.assertEqual(len(FakeSSH.instances), 2, "前提: 2 本目の接続が試された")
        return window, terminal

    def test_a_failed_reconnect_leaves_the_tab_waiting_for_enter(self):
        """再接続に失敗したら、Enter で再接続できる状態のままであること。"""
        window, terminal = self._after_failed_reconnect()

        self.assertFalse(terminal.can_send_input(),
                         "再接続に失敗したのに入力が通る状態になっている")
        fired = []
        terminal.reconnect_requested.connect(lambda: fired.append(1))
        self._type(terminal, Qt.Key.Key_Return, "\r")

        self.assertEqual(fired, [1],
                         "画面の案内どおりに Enter を押しても再接続が起きない")

    def test_keystrokes_are_not_handed_to_the_failed_connection(self):
        """失敗した接続へ打鍵を渡さないこと。"""
        window, terminal = self._after_failed_reconnect()
        conn2 = FakeSSH.instances[1]

        self._type(terminal, Qt.Key.Key_A, "a")

        self.assertEqual(conn2.sent, [], "破棄された接続へ打鍵が渡っている")

    def test_enter_can_retry_after_a_reconnect_by_enter_failed(self):
        """Enter での再接続に失敗しても、もう一度 Enter で再接続できること。

        keyPressEvent が結果を待たずに待機を解いていたため、1 回目の Enter
        で再接続が失敗すると待機を張り直す者がおらず、画面に残る案内
        （「Enterキーを押すと再接続します」）どおりに押しても何も起きな
        かった。復旧には接続ボタンかタブの閉じ直しが要った。
        """
        window, terminal = self._connected_then_dropped()

        self._type(terminal, Qt.Key.Key_Return, "\r")   # 1 回目は失敗する
        self._pump()
        self.assertEqual(len(FakeSSH.instances), 2,
                         "前提: Enter で再接続が試された")

        FakeSSH.always_succeed = True
        self._type(terminal, Qt.Key.Key_Return, "\r")   # 2 回目
        self._pump()

        self.assertEqual(len(FakeSSH.instances), 3,
                         "案内どおりに Enter を押しても再接続が起きない")
        self.assertTrue(terminal.can_send_input(),
                        "再接続に成功したのに入力が禁止されたまま")

    def test_keystrokes_are_not_sent_after_a_failed_reconnect_by_enter(self):
        """Enter での再接続に失敗した後、打鍵を破棄済みの接続へ渡さないこと。"""
        window, terminal = self._connected_then_dropped()

        self._type(terminal, Qt.Key.Key_Return, "\r")
        self._pump()
        self.assertEqual(len(FakeSSH.instances), 2,
                         "前提: Enter で再接続が試された")

        self.assertFalse(terminal.can_send_input(),
                         "再接続に失敗したのに入力が通る状態になっている")
        self._type(terminal, Qt.Key.Key_A, "a")
        self._pump()
        self.assertEqual(FakeSSH.instances[1].sent, [],
                         "破棄された接続へ打鍵が渡っている")

    def test_a_successful_reconnect_still_clears_the_wait(self):
        """接続に成功したら、これまでどおり待ちが解けること。"""
        window, terminal = self._connected_then_dropped()
        FakeSSH.always_succeed = True

        self._press_connect_button(window)

        self.assertEqual(len(FakeSSH.instances), 2, "前提: 2 本目の接続ができた")
        self.assertTrue(terminal.can_send_input(),
                        "再接続に成功したのに入力が禁止されたまま")
        self._type(terminal, Qt.Key.Key_A, "a")
        self.assertEqual(FakeSSH.instances[1].sent, ["a"],
                         "打鍵が新しい接続へ届いていない")


if __name__ == "__main__":
    unittest.main()
