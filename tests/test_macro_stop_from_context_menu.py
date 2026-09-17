"""実行中のコマンドリスト（マクロ）を右クリックメニューから止められることを検証する。

MacroManager には stop_command_list があり MainWindow の接続も済んでいたが、
それを発火する部品がダイアログにも右クリックメニューにもメニューバーにも
無かった（実測: メニュー項目は 'マクロ実行' のみ、Ctrl+C は機器へ \\x03 が
届くだけでタイマーは cmd2, cmd3 をそのまま送った）。誤ったマクロを流し
始めたとき、タブを閉じる以外に中断する手段が無い。

実行中のときだけ右クリックメニューに「マクロ停止」を出し、選ぶと
そのタブの機器のコマンドリストを止める。

端末の右クリックは Tera Term と同じく貼り付けになったので、「マクロ停止」は
接続先リストの機器メニュー「ツール」へ移った（利用者判断 2026-09-17）。
ここでは移った先で同じことができるかを確かめる。
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


class MacroStopFromContextMenuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-ms-")))
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
        window.device_tree.load_from_config([{"name": "Lab", "devices": [device]}])
        window._on_connect_requested(device)
        self._pump()
        terminal = window.terminal_widget._terminals["dev"]
        self.assertTrue(terminal.can_send_input(), "前提: 接続できている")
        conn = FakeSSH.instances[-1]
        del conn.sent[:]
        return window, terminal, conn

    @staticmethod
    def _context_menu_actions(window):
        """接続先リストで機器を右クリックしたつもりで、「ツール」の項目（テキスト → QAction）を集める。"""
        captured = {}

        def fake_exec(menu, *args, **kwargs):
            tools = next(a for a in menu.actions() if a.text() == "ツール").menu()
            captured["actions"] = {a.text(): a for a in tools.actions()}
            return None

        tree = window.device_tree
        item = tree.tree.topLevelItem(0).child(0)
        with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
            tree._show_context_menu(tree.tree.visualItemRect(item).center())
        return captured["actions"]

    def test_stop_item_is_absent_while_no_macro_runs(self):
        window, terminal, conn = self._connected_window()
        self.assertNotIn("マクロ停止", self._context_menu_actions(window))

    def test_stop_item_appears_and_stops_the_running_macro(self):
        """実行中は「マクロ停止」が出て、選ぶと残りのコマンドが送られないこと。"""
        window, terminal, conn = self._connected_window()
        window.macro_manager.start_command_list(
            "dev", ["cmd1", "cmd2", "cmd3"], 200)
        self.assertEqual(conn.sent, ["cmd1\r"], "前提: 最初のコマンドは即送られる")

        actions = self._context_menu_actions(window)
        self.assertIn("マクロ停止", actions,
                      "実行中なのに止める項目が無い: %s" % sorted(actions))
        actions["マクロ停止"].trigger()

        self.assertFalse(window.macro_manager.is_command_list_active("dev"))
        self._pump(0.6)
        self.assertEqual(conn.sent, ["cmd1\r"],
                         "停止したのに残りのコマンドが送られた: %r" % conn.sent)
        self.assertNotIn("マクロ停止", self._context_menu_actions(window),
                         "止めた後も項目が残っている")

    def test_stop_item_disappears_when_the_macro_finishes(self):
        window, terminal, conn = self._connected_window()
        window.macro_manager.start_command_list("dev", ["cmd1"], 50)
        self.assertIn("マクロ停止", self._context_menu_actions(window))
        self._pump(0.4)
        self.assertFalse(window.macro_manager.is_command_list_active("dev"),
                         "前提: マクロは終わっている")
        self.assertNotIn("マクロ停止", self._context_menu_actions(window),
                         "終わった後も項目が残っている")


if __name__ == "__main__":
    unittest.main()
