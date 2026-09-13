"""マクロとキープアライブの送信が、貼り付けの途中へ割り込まないことを検証する。

貼り付けは InteractiveTerminal の送信キューで 512 文字ずつ、区切りごとに
イベントループへ譲りながら流す。一方 MacroManager の送信コールバックは
接続の send_command へ直結されていたため、キューを通らず、貼り付けの
チャンク間に割り込んだ（実測: 512 文字 → 'show version\\r' → 512 文字の
順で機器へ届き、キープアライブの CR も 102 送信中の 45 番目と 88 番目へ
入った）。途中で CR が入ると、未完成の設定行がその場で実行される。

マクロ・キープアライブの送信も、打鍵や貼り付けと同じ送信キューへ積む。
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


class MacroSendsThroughTerminalQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-mq-")))
        ap.start()
        self.addCleanup(ap.stop)
        FakeSSH.instances = []
        self.ssh_patch = mock.patch("ui.main_window.SSHConnection", FakeSSH)
        self.ssh_patch.start()
        self.addCleanup(self.ssh_patch.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _connected_window(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        device = {"name": "dev", "host": "192.0.2.10", "port": 22,
                  "username": "u", "password": "", "protocol": "ssh"}
        window._on_connect_requested(device)
        self._pump()
        terminal = window.terminal_widget._terminals["dev"]
        self.assertTrue(terminal.can_send_input(), "前提: 接続できている")
        conn = FakeSSH.instances[-1]
        del conn.sent[:]
        return window, terminal, conn

    def _start_long_paste(self, terminal, conn):
        """貼り付けを始め、最初のひと区切りだけが送られた状態にする。"""
        chunk = terminal.SEND_CHUNK
        terminal.send_text("P" * (chunk * 2))
        self.assertEqual(conn.sent, ["P" * chunk],
                         "前提: 最初のひと区切りだけが同期で送られる")

    def test_a_macro_started_mid_paste_waits_for_the_paste(self):
        """右クリックのマクロ実行が、貼り付けの残りを追い越さないこと。"""
        window, terminal, conn = self._connected_window()
        chunk = terminal.SEND_CHUNK
        self._start_long_paste(terminal, conn)

        with mock.patch.object(window.config_manager, "get_macro_by_name",
                               return_value={"name": "m1",
                                             "commands": ["show version"]}):
            window._on_macro_execute_requested("dev", "m1")
        self._pump()

        self.assertEqual(conn.sent, ["P" * chunk, "P" * chunk, "show version\r"],
                         "マクロが貼り付けのチャンク間に割り込んだ: %r"
                         % [(len(s), s[:12]) for s in conn.sent])

    def test_a_keepalive_fired_mid_paste_waits_for_the_paste(self):
        """キープアライブの CR が貼り付けの途中へ入らないこと。"""
        window, terminal, conn = self._connected_window()
        chunk = terminal.SEND_CHUNK
        window._start_keepalive("dev", 60)
        self._start_long_paste(terminal, conn)

        window.macro_manager._send_keepalive("dev")     # タイマー発火を模す
        self._pump()

        self.assertEqual(conn.sent, ["P" * chunk, "P" * chunk, "\r"],
                         "キープアライブが貼り付けのチャンク間に割り込んだ: %r"
                         % [(len(s), s[:12]) for s in conn.sent])

    def test_a_macro_still_reaches_the_device_when_nothing_is_queued(self):
        """貼り付け中でなければ、これまでどおり即座に届くこと。"""
        window, terminal, conn = self._connected_window()
        with mock.patch.object(window.config_manager, "get_macro_by_name",
                               return_value={"name": "m1",
                                             "commands": ["show clock"]}):
            window._on_macro_execute_requested("dev", "m1")
        self.assertEqual(conn.sent, ["show clock\r"])


if __name__ == "__main__":
    unittest.main()
