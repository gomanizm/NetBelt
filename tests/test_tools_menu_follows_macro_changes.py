"""接続したあとにプリセット（マクロ）を削除・追加しても、接続先リストの
「ツール → マクロ実行」が設定と同じ一覧を出すこと。

「ツール」の一覧は、接続したときに端末へ渡した global_macros のリストを
端末が持ち続けて出していた。ConfigManager.remove_global_macro はそのリストを
書き換えずに新しいリストへ差し替えるので、削除したあとは端末だけが古い
リストを見続ける。config.json に global_macros キーが無いときも、
get_global_macros が保存されない [] を返すので同じことになる。

実測: preset-A（説明 '旧A'、show a）だけを持った状態で rtr1 に接続し、
メニューバーのツール → マクロ設定で preset-A を削除して preset-C を新規
作成すると、設定は ['preset-C'] なのに「ツール」は ['preset-A - 旧A'] の
ままで C は出なかった。A を選ぶと『マクロ 'preset-A' が見つかりません。』。
同じ名前 preset-A を reload・説明 '新A' で作り直しても表示は『preset-A -
旧A』のままで、それを選ぶと reload が送られた（古い説明を見て新しい定義が
実行される）。

直し方: MainWindow が「ツール」へ渡す状態のうち、マクロの一覧だけは開く
たびに config_manager.get_global_macros() から取り直す。
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


def _macro_items(tree, device_item, choose=None):
    """機器の右クリック →「ツール」→「マクロ実行」に並ぶ項目の文字列

    choose を渡すと、その項目を選んだことにする。
    """
    captured = {}

    def fake_exec(menu, *args, **kwargs):
        tools = next(a.menu() for a in menu.actions() if a.text() == "ツール")
        run = [a.menu() for a in tools.actions() if a.text() == "マクロ実行"]
        actions = run[0].actions() if run else []
        captured["items"] = [a.text() for a in actions]
        if choose is None:
            return None
        action = next(a for a in actions if a.text() == choose)
        # 本物の exec と同じく、選ばれた項目の triggered を出してから返す
        action.trigger()
        return action

    pos = tree.tree.visualItemRect(device_item).center()
    with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
        tree._show_context_menu(pos)
    return captured["items"]


class ToolsMenuFollowsMacroChangesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-tools-macros-")
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(self.dir))
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

    def _connected_window(self, drop_macro_key=False):
        """preset-A（説明 '旧A'）だけを持った状態で dev に接続したメインウィンドウ"""
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.dir, "config.json"))
            window = MainWindow()
        self.addCleanup(window.close)
        cm = window.config_manager
        if drop_macro_key:
            # 手で編集した config.json などで global_macros キーが無い
            cm.config.pop("global_macros", None)
        else:
            for macro in list(cm.get_global_macros()):
                self.assertTrue(cm.remove_global_macro(macro["name"]))
            self.assertTrue(cm.add_global_macro("preset-A", ["show a"], "旧A"))
        device = {"name": "dev", "host": "192.0.2.10", "port": 22,
                  "username": "u", "password": "", "protocol": "ssh"}
        window.device_tree.load_from_config([{"name": "Lab", "devices": [device]}])
        window._on_connect_requested(device)
        self._pump()
        self.assertTrue(window.terminal_widget._terminals["dev"].can_send_input(),
                        "前提: 接続できている")
        del FakeSSH.instances[-1].sent[:]
        item = window.device_tree.tree.topLevelItem(0).child(0)
        return window, item

    def test_deleted_and_added_presets_show_up_in_the_tools_menu(self):
        """削除したプリセットは消え、追加したプリセットが出ること。"""
        window, item = self._connected_window()
        self.assertEqual(_macro_items(window.device_tree, item),
                         ["preset-A - 旧A"], "前提: 接続時の一覧")
        cm = window.config_manager
        self.assertTrue(cm.remove_global_macro("preset-A"))
        self.assertTrue(cm.add_global_macro("preset-C", ["show c"], "新C"))

        self.assertEqual(_macro_items(window.device_tree, item),
                         ["preset-C - 新C"])

    def test_recreated_preset_shows_its_new_description(self):
        """同じ名前で作り直したら、新しい説明で出ること（選べば新しい定義が送られる）。"""
        window, item = self._connected_window()
        cm = window.config_manager
        self.assertTrue(cm.remove_global_macro("preset-A"))
        self.assertTrue(cm.add_global_macro("preset-A", ["show b"], "新A"))

        self.assertEqual(_macro_items(window.device_tree, item),
                         ["preset-A - 新A"])
        _macro_items(window.device_tree, item, choose="preset-A - 新A")
        self._pump(0.2)
        self.assertEqual(FakeSSH.instances[-1].sent[:1], ["show b\r"])
        window.macro_manager.cleanup_device("dev")

    def test_presets_added_when_config_had_no_macro_list(self):
        """global_macros キーが無い設定でも、接続後に追加したものが出ること。"""
        window, item = self._connected_window(drop_macro_key=True)
        self.assertEqual(_macro_items(window.device_tree, item), [],
                         "前提: プリセットは 1 つも無い")
        self.assertTrue(window.config_manager.add_global_macro(
            "preset-X", ["show x"], "X"))

        self.assertEqual(_macro_items(window.device_tree, item), ["preset-X - X"])


if __name__ == "__main__":
    unittest.main()
