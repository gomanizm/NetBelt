"""Enter で再接続を求めても、再接続待ちがターミナル側で解けないことを固定する。

InteractiveTerminal.keyPressEvent は、再接続待ち（_reconnect_mode）のまま
reconnect_requested を出す。待ちを解くのは接続に成功した時点
（MainWindow._on_connection_success）だけである。

既存の tests/test_reconnect_wait_survives_enter_reconnect.py は
MainWindow._on_connection_error まで通す。あちらは失敗のたびに
enable_reconnect で待ちを張り直すので、ターミナル側が Enter で待ちを
落としてしまっても振る舞いが変わらず、この 1 行を守れていない。

ここでは 2 つの面から固定する。

1. ウィジェット単体。誰も張り直してくれない状態で、Enter のあとも待ちが
   残り、打鍵が機器へ流れないこと。
2. _on_connection_error を通らない失敗経路。再接続情報が見つからず
   MainWindow._reconnect_device が何もせずに戻った場合でも、画面の案内
   どおりにもう一度 Enter を押せること。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent

sys.path.insert(0, "src")


def _type(terminal, key, text):
    terminal.keyPressEvent(QKeyEvent(
        QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text))


class ReconnectModePersistsAfterEnterTest(unittest.TestCase):
    """ウィジェット単体での固定。MainWindow は関与しない。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _waiting_terminal(self):
        from ui.terminal_widget import InteractiveTerminal
        terminal = InteractiveTerminal()
        self.addCleanup(terminal.deleteLater)
        terminal.set_reconnect_mode(True)
        self.assertFalse(terminal.can_send_input(), "前提: 再接続待ちでは送れない")
        return terminal

    def test_enter_asks_for_reconnect_without_leaving_the_wait(self):
        """Enter を押しても待ちが残ること（張り直す相手がいなくても）。"""
        terminal = self._waiting_terminal()
        fired = []
        terminal.reconnect_requested.connect(lambda: fired.append(1))

        _type(terminal, Qt.Key.Key_Return, "\r")

        self.assertEqual(fired, [1], "前提: Enter で再接続が要求される")
        self.assertFalse(
            terminal.can_send_input(),
            "Enter だけで再接続待ちが解けている（接続に成功していないのに）")

    def test_enter_can_be_pressed_again_while_the_wait_holds(self):
        """待ちが残る以上、Enter は何度でも再接続要求になること。"""
        terminal = self._waiting_terminal()
        fired = []
        terminal.reconnect_requested.connect(lambda: fired.append(1))

        _type(terminal, Qt.Key.Key_Return, "\r")
        _type(terminal, Qt.Key.Key_Return, "\r")

        self.assertEqual(fired, [1, 1],
                         "2 回目の Enter が再接続要求にならない")

    def test_keystrokes_after_enter_are_not_sent(self):
        """Enter のあとの打鍵が、切れたままの接続へ流れないこと。"""
        terminal = self._waiting_terminal()
        sent = []
        terminal.key_pressed.connect(sent.append)

        _type(terminal, Qt.Key.Key_Return, "\r")
        _type(terminal, Qt.Key.Key_A, "a")

        self.assertEqual(sent, [], "再接続待ちのはずなのに打鍵が送られた: %r" % sent)

    def test_paste_after_enter_is_refused(self):
        """打鍵と同じく、貼り付け経路も塞がったままであること。"""
        terminal = self._waiting_terminal()
        sent = []
        terminal.key_pressed.connect(sent.append)

        _type(terminal, Qt.Key.Key_Return, "\r")

        self.assertFalse(terminal.send_text("show version\r"),
                         "再接続待ちのはずなのに貼り付けが受け付けられた")
        self.assertEqual(sent, [], "貼り付けが送られた: %r" % sent)


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
        type(self).instances.append(self)

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


class ReconnectWaitSurvivesMissingDeviceInfoTest(unittest.TestCase):
    """_on_connection_error を通らない失敗経路での固定。

    再接続情報が消えていると MainWindow._reconnect_device は案内を出して
    戻るだけで、enable_reconnect を呼ばない。ターミナル側が Enter で待ちを
    落としていると、ここで待ちを張り直す者がいなくなる。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-reconnmode-")))
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

    def _waiting_tab_without_device_info(self):
        """接続 → 切断 → 再接続情報が消えた、の状態まで進める。"""
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        window._on_connect_requested(self.device)
        self._pump()
        terminal = window.terminal_widget._terminals["dev"]
        self.assertTrue(terminal.can_send_input(), "前提: 初回接続後は入力できる")

        window._on_connection_closed("dev")
        self.assertFalse(terminal.can_send_input(), "前提: 切断後は入力禁止")
        window.device_info.pop("dev", None)
        return window, terminal

    def test_enter_still_works_after_a_reconnect_that_never_started(self):
        """再接続が始まりもしなかったあとも、Enter が効き続けること。"""
        window, terminal = self._waiting_tab_without_device_info()

        _type(terminal, Qt.Key.Key_Return, "\r")
        self._pump(0.1)
        self.assertIn("再接続情報が見つかりません", terminal.toPlainText(),
                      "前提: 再接続は始まらなかった")
        self.assertEqual(len(FakeSSH.instances), 1,
                         "前提: 新しい接続は作られていない")

        fired = []
        terminal.reconnect_requested.connect(lambda: fired.append(1))
        _type(terminal, Qt.Key.Key_Return, "\r")

        self.assertEqual(fired, [1],
                         "画面の案内どおりに Enter を押しても再接続が起きない")

    def test_keystrokes_are_not_handed_to_the_closed_connection(self):
        """待ちが解けていないので、打鍵は捨てた接続へ渡らないこと。"""
        window, terminal = self._waiting_tab_without_device_info()
        conn = FakeSSH.instances[0]

        _type(terminal, Qt.Key.Key_Return, "\r")
        self._pump(0.1)
        del conn.sent[:]
        _type(terminal, Qt.Key.Key_A, "a")

        self.assertEqual(conn.sent, [], "切れた接続へ打鍵が渡っている: %r" % conn.sent)


if __name__ == "__main__":
    unittest.main()
