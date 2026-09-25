"""「ツール」を開いている間に切れた機器でマクロを選んでも、「実行中」と出さないことを検証する。

何が起きていたか（実測、441ea02）: 接続中の rtrA で右クリック →「ツール」→
「マクロ実行」の m1 を掴んだまま相手機器が切れ、そこで m1 を選ぶと、
ステータスバーに「マクロ 'm1' を実行中...」と出た。実際には送信先が無いので
MacroManager が macro_error（'コマンド送信コールバックが登録されていません'）を
出してすぐ止めており（is_command_list_active は False）、何も送られていない。
macro_error はどこにも繋がっていないので、利用者には「実行中」だけが見えた。
同じ場面でキープアライブ開始を選ぶと『rtrA: 接続が切れているため
キープアライブを開始できません』と出る。

利用者の決定（b）: MainWindow._on_macro_execute_requested で、start_command_list
のあとに実行が始まっているかを確かめ、始まったときだけ「実行中」と出す。
始まらなかったときは『rtrA: 接続が切れているためマクロを実行できません』と出す。
受け口での接続の確認（キープアライブと同じ形）は、既存のテストが接続の無い
機器名で送れることを前提にしているので入れない。
"""
import json
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
        self.disconnected.emit()


DEVICE = {"name": "rtrA", "host": "192.0.2.10", "port": 22, "protocol": "ssh",
          "username": "u", "password": "", "ssh_key": "", "macros": []}
MACROS = [{"name": "m1", "commands": ["show clock"], "description": ""}]


def _grab_macro_action(tree, device_name, text):
    """機器の右クリック →「ツール」→「マクロ実行」の項目を掴んだまま返す"""
    root = tree.tree.invisibleRootItem()
    item = None
    for i in range(root.childCount()):
        group = root.child(i)
        for j in range(group.childCount()):
            data = group.child(j).data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and data.get("name") == device_name:
                item = group.child(j)
    captured = {}

    def walk(menu):
        for action in menu.actions():
            if action.menu() is not None:
                walk(action.menu())
            elif action.text() == text:
                captured["action"] = action

    def fake_exec(menu, *args, **kwargs):
        walk(menu)
        return None

    with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
        tree._show_context_menu(tree.tree.visualItemRect(item).center())
    return captured.get("action")


class MacroStatusAfterMenuDisconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-macro-stale-")
        FakeSSH.instances = []
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir)),
                mock.patch("ui.device_tree.list_serial_ports", return_value=[]),
                mock.patch("ui.main_window.SSHConnection", FakeSSH)):
            patch.start()
            self.addCleanup(patch.stop)

    def _pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def _discard(window):
        for name in list(window.connections):
            window.macro_manager.stop_command_list(name)
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _connected_window(self):
        """rtrA へ接続済みで、グローバルマクロ m1 を持つメインウィンドウ"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"config_version": "1.0",
                       "groups": [{"name": "Default", "auto_commands": [],
                                   "devices": [dict(DEVICE)]}],
                       "global_macros": MACROS, "settings": {}}, f)
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(config_path=path)
            window = MainWindow()
        self.addCleanup(self._discard, window)
        window._on_connect_requested(dict(DEVICE))
        self._pump()
        self.assertIn("rtrA", window.connections, "前提: 接続できている")
        return window

    def test_a_macro_chosen_after_the_drop_does_not_claim_to_run(self):
        """切断後に掴んでおいたマクロを選ぶと、「実行中」ではなく理由を出すこと"""
        window = self._connected_window()
        action = _grab_macro_action(window.device_tree, "rtrA", "m1")
        self.assertIsNotNone(action, "前提: 「マクロ実行」の m1 ができた")

        FakeSSH.instances[-1].disconnected.emit()
        self._pump()
        self.assertNotIn("rtrA", window.connections, "前提: 切断されている")
        window.status_bar.clearMessage()

        with mock.patch("ui.main_window.QMessageBox") as box:
            action.trigger()
        self._pump(0.1)

        message = window.status_bar.currentMessage()
        self.assertNotIn("実行中", message, "切れているのに実行中と出た: %r" % message)
        self.assertIn("rtrA", message, "どの機器の話か分からない: %r" % message)
        self.assertIn("接続が切れているため", message,
                      "実行できなかった理由を出していない: %r" % message)
        self.assertFalse(window.macro_manager.is_command_list_active("rtrA"))
        self.assertEqual(box.method_calls, [], "モーダルは出さない")

    def test_a_macro_on_a_live_session_still_says_running(self):
        """対照: 繋がっていれば、これまでどおり送って「実行中」と出すこと"""
        window = self._connected_window()
        action = _grab_macro_action(window.device_tree, "rtrA", "m1")
        conn = FakeSSH.instances[-1]
        del conn.sent[:]

        action.trigger()
        self._pump(0.1)

        self.assertIn("マクロ 'm1' を実行中", window.status_bar.currentMessage())
        self.assertEqual(conn.sent, ["show clock\r"])


if __name__ == "__main__":
    unittest.main()
