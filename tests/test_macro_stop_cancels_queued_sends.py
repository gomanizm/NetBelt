"""マクロ停止が、まだ送っていないコマンドも取り消すことを検証する。

マクロの送信はターミナルの送信キューを通るようになった（貼り付けの
チャンク間へ割り込ませないため）。ところが stop_command_list は
タイマーと自分の辞書を消すだけで、キューに積んだぶんには触らない。
長い貼り付けを流している最中にマクロを始めて止めると、停止したのに
残りのコマンドが遅れて機器へ届く。

停止のときだけ、その機器のキューからマクロ由来の断片を取り除く。
打鍵・貼り付けと、最後まで走り切ったマクロのぶんは巻き添えにしない。
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


class MacroStopCancelsQueuedSendsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-mc-")))
        ap.start()
        self.addCleanup(ap.stop)
        FakeSSH.instances = []
        ssh_patch = mock.patch("ui.main_window.SSHConnection", FakeSSH)
        ssh_patch.start()
        self.addCleanup(ssh_patch.stop)

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
        self.addCleanup(window.macro_manager.cleanup_device, "dev")
        return window, terminal, conn

    def _start_long_paste(self, terminal, conn):
        """貼り付けを始め、最初のひと区切りだけが送られた状態にする。"""
        chunk = terminal.SEND_CHUNK
        terminal.send_text("P" * (chunk * 2))
        self.assertEqual(conn.sent, ["P" * chunk],
                         "前提: 最初のひと区切りだけが同期で送られる")

    def test_stopping_a_macro_drops_the_commands_left_in_the_queue(self):
        """排出中に始めたマクロを止めたら、残りが遅れて届かないこと。"""
        window, terminal, conn = self._connected_window()
        chunk = terminal.SEND_CHUNK
        self._start_long_paste(terminal, conn)

        window.macro_manager.start_command_list(
            "dev", ["cmd1", "cmd2", "cmd3"], 50)
        self.assertEqual(conn.sent, ["P" * chunk],
                         "前提: 1本目は貼り付けの後ろへ積まれ、まだ届かない")

        window.macro_manager.stop_command_list("dev")
        self._pump(0.6)

        self.assertEqual(conn.sent, ["P" * chunk, "P" * chunk],
                         "停止したのにマクロのコマンドが届いた: %r"
                         % [(len(s), s[:12]) for s in conn.sent])

    def test_stopping_a_macro_keeps_the_paste_that_was_already_queued(self):
        """停止で、打鍵や貼り付けまで巻き添えに消えないこと。"""
        window, terminal, conn = self._connected_window()
        chunk = terminal.SEND_CHUNK
        self._start_long_paste(terminal, conn)

        window.macro_manager.start_command_list("dev", ["cmd1", "cmd2"], 50)
        terminal.send_text("typed\r")
        window.macro_manager.stop_command_list("dev")
        self._pump(0.6)

        self.assertEqual(conn.sent, ["P" * chunk, "P" * chunk, "typed\r"],
                         "貼り付け・打鍵まで消えた: %r"
                         % [(len(s), s[:12]) for s in conn.sent])

    def test_a_macro_that_finishes_still_delivers_its_queued_commands(self):
        """走り切ったマクロのぶんは、積んだままでも届くこと。"""
        window, terminal, conn = self._connected_window()
        chunk = terminal.SEND_CHUNK
        self._start_long_paste(terminal, conn)

        window.macro_manager.start_command_list("dev", ["cmd1"], 50)
        self._pump(0.6)

        self.assertFalse(window.macro_manager.is_command_list_active("dev"),
                         "前提: マクロは終わっている")
        self.assertEqual(conn.sent, ["P" * chunk, "P" * chunk, "cmd1\r"],
                         "終わったマクロのコマンドまで消えた: %r"
                         % [(len(s), s[:12]) for s in conn.sent])


if __name__ == "__main__":
    unittest.main()
