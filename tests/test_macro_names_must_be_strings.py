"""マクロの隔離と読み手側の保険が、名前の型まで見ることを検証する。

何が起きていたか（実測）: global_macros と機器の macros をそろえる判定も、
3 つの読み手（DeviceTree の「ツール」・マクロ設定の一覧・機器編集の一覧）の
保険も、どれも「辞書かどうか」しか見ていなかった。辞書ではあるが name が
文字列でない要素はそのまま通り、名前を画面へ入れるところで全部落ちる。
config.json に

    "global_macros": [{"name": 5, "commands": ["show version"], "description": ""}]

を置いて起動すると load_error は None・警告も無し（get_global_macros() は
[{'name': 5, ...}] をそのまま返す）で、そのうえで

  - DeviceTree._add_tools_menu → QMenu.addAction(5) で TypeError
    （接続中の機器の右クリックが一切開けない）
  - MacroDialog._load_presets → addItem(5) で TypeError（マクロ設定が開けない）
  - DeviceDialog._load_data → addItem(7) で TypeError
    （機器の macros に同じ形が入ると、その機器を編集で直せない）

となった。辞書でない要素（None や文字列）のときだけ助かる、中途半端な
状態だった。

どう直したか: 隔離の keep 判定を「辞書で、name が文字列」にそろえる
（機器の macros と global_macros の 2 か所）。読めない分を外したことは
これまでどおり警告で知らせる。読み手側 3 か所の保険も同じ条件にそろえて、
手編集の config やこの検査より前のバージョンが作った config から来ても、
右クリック・マクロ設定・機器編集が開けるようにする。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

MACRO = {"name": "show", "commands": ["show version"], "description": ""}
# 辞書ではあるが名前が文字列でない要素（手編集の config から来る形）
NUMERIC_NAME = {"name": 5, "commands": ["show version"], "description": ""}


def _device(name, **extra):
    data = {"name": name, "host": "192.0.2.1", "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}
    data.update(extra)
    return data


class MacroNameTypeQuarantineTest(unittest.TestCase):
    """読み込み時の隔離が、名前の型まで見ること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-macro-name-type-")
        patch = mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir))
        patch.start()
        self.addCleanup(patch.stop)

    def _load(self, global_macros, device_macros=None):
        """その内容を持つ config.json を読み込んだ ConfigManager"""
        from core.config_manager import ConfigManager
        path = os.path.join(self.dir,
                            "config-%d.json" % len(os.listdir(self.dir)))
        device = _device("R1", macros=device_macros or [])
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"config_version": "1.0",
                       "groups": [{"name": "Lab", "auto_commands": [],
                                   "devices": [device]}],
                       "global_macros": global_macros, "settings": {}}, f)
        manager = ConfigManager(config_path=path)
        self.assertIsNone(manager.load_error, "前提: JSON としては読めている")
        return manager

    def test_a_global_macro_without_a_string_name_is_dropped(self):
        """名前が文字列でない全体共通マクロは、読み込みで外すこと。"""
        manager = self._load([NUMERIC_NAME, MACRO])

        self.assertEqual(manager.get_global_macros(), [MACRO],
                         "名前が文字列でないマクロが残っている: %r"
                         % manager.get_global_macros())

    def test_dropping_it_is_reported(self):
        """外した分は、辞書でない要素のときと同じように知らせること。"""
        manager = self._load([NUMERIC_NAME])

        warning = manager.load_warning or ""
        self.assertIn("全体共通マクロ", warning,
                      "全体共通マクロを外したことを知らせていない: %r" % warning)

    def test_a_device_macro_without_a_string_name_is_dropped(self):
        """機器別マクロも同じ条件で外し、知らせること。"""
        manager = self._load([], device_macros=[NUMERIC_NAME, MACRO])

        lab = [g for g in manager.get_groups() if g["name"] == "Lab"]
        self.assertEqual(len(lab), 1, "前提: Lab グループが読めている")
        device = lab[0]["devices"][0]
        self.assertEqual(device["macros"], [MACRO],
                         "名前が文字列でないマクロが残っている: %r"
                         % device["macros"])
        self.assertIn("R1", manager.load_warning or "",
                      "どの機器のマクロを外したか知らせていない: %r"
                      % manager.load_warning)

    def test_good_macros_are_left_alone(self):
        """正しいマクロは、そのまま・警告なしで通すこと。"""
        manager = self._load([MACRO], device_macros=[MACRO])

        self.assertEqual(manager.get_global_macros(), [MACRO])
        self.assertIsNone(manager.load_warning,
                          "正しいマクロで警告が出た: %r" % manager.load_warning)


class MacroNameTypeReaderGuardTest(unittest.TestCase):
    """読み手側の保険: 名前が文字列でない要素が来ても、画面が開くこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_tools_menu_skips_macros_without_a_string_name(self):
        """接続中の機器の右クリックが開き、読めるマクロだけ項目になること。"""
        from PyQt6.QtWidgets import QMenu
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        # 1 秒ごとにシリアルポートを見に行くタイマーを持つので、止めておく。
        # 止めないと、この試験一式の残り全部の間も動き続ける
        tree._serial_monitor_timer.stop()
        self.addCleanup(tree.close)
        tree.set_tools_state_provider(
            lambda name: {"connected": True, "keepalive_active": False,
                          "command_list_active": False,
                          "macros": [NUMERIC_NAME, MACRO]})
        menu = QMenu()
        self.addCleanup(menu.deleteLater)

        tree._add_tools_menu(menu, "R1")

        tools = menu.actions()[0].menu()
        macro_menu = [a.menu() for a in tools.actions()
                      if a.text() == "マクロ実行"]
        self.assertEqual(len(macro_menu), 1, "前提: 「マクロ実行」が出ている")
        self.assertEqual([a.text() for a in macro_menu[0].actions()], ["show"],
                         "読めない要素まで項目になっている")

    def test_the_macro_dialog_skips_macros_without_a_string_name(self):
        """マクロ設定が開き、読めるプリセットだけ一覧に出ること。"""
        from ui.dialogs.macro_dialog import MacroDialog
        config_manager = mock.Mock()
        config_manager.get_global_macros.return_value = [NUMERIC_NAME, MACRO]

        dialog = MacroDialog(None, device_name="R1",
                             config_manager=config_manager)
        self.addCleanup(dialog.deleteLater)

        names = [dialog.preset_list_widget.item(i).text()
                 for i in range(dialog.preset_list_widget.count())]
        self.assertEqual(names, ["show"], "読めない要素まで一覧に出ている")

    def test_the_device_dialog_skips_macros_without_a_string_name(self):
        """機器編集が開き、その機器を編集で直せること。"""
        from ui.dialogs.device_dialog import DeviceDialog
        device = _device("R1", macros=[{"name": 7, "commands": [],
                                        "description": ""}, MACRO])

        dialog = DeviceDialog(None, groups=["Lab"], device_data=device)
        self.addCleanup(dialog.deleteLater)

        names = [dialog.macro_list.item(i).text()
                 for i in range(dialog.macro_list.count())]
        self.assertEqual(names, ["show"], "読めない要素まで一覧に出ている")


if __name__ == "__main__":
    unittest.main()
