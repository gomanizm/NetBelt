"""3 つ目の任意リスト（最上位の global_macros）も、壊れた config.json からそろえることを検証する。

何が起きていたか（実測）: 読み込み時の隔離が見ていたのは機器の macros と
グループの auto_commands だけで、全体共通マクロは素通りしていた。
ConfigManager.get_global_macros は self.config.get("global_macros", []) を
そのまま返すので、"global_macros": null の config.json では None が返る。

    load_error: None                    ← 警告も出ない
    _macro_list: None
    _tools_state_for RAISED: TypeError("'NoneType' object is not iterable")

接続中の機器を右クリックすると「ツール」の状態を作る
MainWindow._tools_state_for が list(None) で落ち、excepthook の「予期しない
エラー」になる。要素が辞書でない場合も、DeviceTree._add_tools_menu と
MacroDialog._load_presets の for ループが macro.get(...) で
AttributeError になった（機器の macros と違い、読み手側の保険も無かった）。

どう直したか: 隔離（_quarantine_invalid_devices）で config 直下の
global_macros も _normalize_optional_list に通す。null は「無し」と同じ
意味なので黙ってそろえ、リストでない値とリストの中の読めない要素は外して
警告で知らせる（機器の macros と同じ扱い。名前が無いので「全体共通マクロ」
という固定の呼び名を使う）。読み手側の保険として、上の 2 つの for ループでも
辞書でない要素を飛ばす。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, pyqtSignal

sys.path.insert(0, "src")

MACRO = {"name": "show", "commands": ["show version"], "description": ""}


class FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。connect() は即座に成功を知らせる。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        pass

    def dispose(self):
        pass

    def disconnect(self):
        pass


def _device(name, **extra):
    data = {"name": name, "host": "192.0.2.1", "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}
    data.update(extra)
    return data


class GlobalMacrosNormalizedTest(unittest.TestCase):
    """読み込み時の隔離が global_macros もそろえること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-global-macros-")
        patch = mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir))
        patch.start()
        self.addCleanup(patch.stop)

    def _config_path(self, global_macros):
        path = os.path.join(self.dir,
                            "config-%d.json" % len(os.listdir(self.dir)))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"config_version": "1.0",
                       "groups": [{"name": "Lab", "auto_commands": [],
                                   "devices": [_device("R1")]}],
                       "global_macros": global_macros, "settings": {}}, f)
        return path

    def _load(self, global_macros):
        """その global_macros を持つ config.json を読み込んだ ConfigManager"""
        from core.config_manager import ConfigManager
        manager = ConfigManager(config_path=self._config_path(global_macros))
        self.assertIsNone(manager.load_error, "前提: JSON としては読めている")
        return manager

    def test_broken_global_macros_are_normalized_on_load(self):
        """リストでなければ空にし、読めない要素は外すこと（機器の macros と同じ）。"""
        cases = [(None, []), ([None, MACRO], [MACRO]), ("show ver", []),
                 (5, []), ([MACRO], [MACRO])]
        for value, expected in cases:
            with self.subTest(value=value):
                manager = self._load(value)
                self.assertEqual(manager.get_global_macros(), expected,
                                 "読み込みで global_macros がそろっていない")

    def test_null_is_normalized_without_a_warning(self):
        """null は「無し」と同じ意味なので、黙ってそろえること。"""
        manager = self._load(None)
        self.assertIsNone(manager.load_warning,
                          "null をそろえただけで警告が出た: %r" % manager.load_warning)
        self.assertIsNone(manager.backup_path,
                          "null をそろえただけでバックアップを取った")

    def test_dropping_unreadable_entries_is_reported(self):
        """外した分は、機器のマクロと同じように知らせること。"""
        manager = self._load([MACRO, "show ver"])
        warning = manager.load_warning or ""
        self.assertIn("全体共通マクロ", warning,
                      "全体共通マクロを外したことを知らせていない: %r" % warning)

    def test_a_good_list_is_left_alone(self):
        """正しいリストは、そのまま・警告なしで通すこと。"""
        manager = self._load([MACRO])
        self.assertEqual(manager.get_global_macros(), [MACRO])
        self.assertIsNone(manager.load_warning)

    def test_the_tools_state_can_be_built_with_null_global_macros(self):
        """接続中の機器を右クリックしたときの状態作りが落ちないこと。"""
        import time
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        path = self._config_path(None)
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.SSHConnection", FakeSSH):
            fake.return_value = ConfigManager(config_path=path)
            window = MainWindow()
            self.addCleanup(window.close)
            window._on_connect_requested(_device("R1"))
            end = time.time() + 0.3
            while time.time() < end:
                self.app.processEvents()
                time.sleep(0.01)

        state = window._tools_state_for("R1")

        self.assertEqual(state.get("macros"), [])


class UnreadableMacroEntriesAreSkippedTest(unittest.TestCase):
    """読み手側の保険: 辞書でない要素が来ても、メニューと一覧が開くこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_tools_menu_skips_entries_that_are_not_macros(self):
        from PyQt6.QtWidgets import QMenu
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        self.addCleanup(tree.close)
        tree.set_tools_state_provider(
            lambda name: {"connected": True, "keepalive_active": False,
                          "command_list_active": False,
                          "macros": [None, "show ver", MACRO]})
        menu = QMenu()
        self.addCleanup(menu.deleteLater)

        tree._add_tools_menu(menu, "R1")

        tools = menu.actions()[0].menu()
        macro_menu = [a.menu() for a in tools.actions()
                      if a.text() == "マクロ実行"]
        self.assertEqual(len(macro_menu), 1, "前提: 「マクロ実行」が出ている")
        self.assertEqual([a.text() for a in macro_menu[0].actions()], ["show"],
                         "読めない要素まで項目になっている")

    def test_the_macro_dialog_skips_entries_that_are_not_macros(self):
        from ui.dialogs.macro_dialog import MacroDialog
        config_manager = mock.Mock()
        config_manager.get_global_macros.return_value = [None, "show ver", MACRO]

        dialog = MacroDialog(None, device_name="R1",
                             config_manager=config_manager)
        self.addCleanup(dialog.deleteLater)

        names = [dialog.preset_list_widget.item(i).text()
                 for i in range(dialog.preset_list_widget.count())]
        self.assertEqual(names, ["show"], "読めない要素まで一覧に出ている")


if __name__ == "__main__":
    unittest.main()
