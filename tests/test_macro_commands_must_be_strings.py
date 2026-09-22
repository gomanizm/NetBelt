"""マクロの隔離が、commands の型まで見ることを検証する。

何が起きていたか（実測）: 名前の型はそろったが、commands の型は誰も見て
いなかった。config.json の global_macros に

    {"name": "bad", "commands": "show version", "description": ""}

を置いて起動すると load_error も load_warning も None のまま、マクロ設定の
一覧にも右クリックの「マクロ実行」にも普通に出る。接続中の機器でそれを選ぶと
MacroManager が文字列をそのまま持ち、commands[index] で 1 文字ずつ取り出す
ため、機器へ 1 文字ずつ CR を付けて送られた（実測: SENT 's' 'h' 'o' 'w'）。
commands が 5（数値）・{"a": "show version"}（辞書）・["show version", 5]
（非文字列を含む list）のときは、送信を仕掛ける QTimer のスロットの中で
TypeError / KeyError になり、PyQt6 はスロット内の未捕捉例外で終了するため
アプリごと落ちた（実測: 終了コード 0xC0000409）。非文字列を含む list では
'show version' を実際に送った後に落ちる＝実機へ 1 行流してからアプリが消える。

同じ形の値を、グループの auto_commands は _is_valid_auto_commands が弾いて
おり、その docstring がまさにこの症状を書いている。マクロにだけ同じ検査が
無かった。

どう直したか: is_readable_macro に commands の型もそろえる（辞書で、name が
文字列で、commands が文字列だけの list）。隔離 2 か所と読み手 3 か所が同じ
関数を呼んでいるので、手編集の config から来た分は読み込み時に外れて
「全体共通マクロ」「機器名」の警告になり、3 つの読み手にも出てこなくなる。
commands キーが無いマクロは既定値 [] で通し、説明だけのマクロは落とさない。
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
# 手編集の config.json から来る形。どれも送信側が list の要素として扱えない
STRING_COMMANDS = {"name": "bad-str", "commands": "show version",
                   "description": ""}
INT_COMMANDS = {"name": "bad-int", "commands": 5, "description": ""}
DICT_COMMANDS = {"name": "bad-dict", "commands": {"a": "show version"},
                 "description": ""}
LIST_WITH_INT = {"name": "bad-list", "commands": ["show version", 5],
                 "description": ""}
BROKEN = [STRING_COMMANDS, INT_COMMANDS, DICT_COMMANDS, LIST_WITH_INT]


def _device(name, **extra):
    data = {"name": name, "host": "192.0.2.1", "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}
    data.update(extra)
    return data


class MacroCommandsTypeQuarantineTest(unittest.TestCase):
    """読み込み時の隔離が、commands の型まで見ること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-macro-cmd-type-")
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

    def test_global_macros_with_unsendable_commands_are_dropped(self):
        """送れない形の commands を持つ全体共通マクロは、読み込みで外すこと。"""
        for broken in BROKEN:
            with self.subTest(commands=broken["commands"]):
                manager = self._load([broken, MACRO])

                self.assertEqual(manager.get_global_macros(), [MACRO],
                                 "送れない commands のマクロが残っている: %r"
                                 % manager.get_global_macros())

    def test_dropping_it_is_reported(self):
        """外した分は、名前が読めないときと同じように知らせること。"""
        manager = self._load([STRING_COMMANDS])

        warning = manager.load_warning or ""
        self.assertIn("全体共通マクロ", warning,
                      "全体共通マクロを外したことを知らせていない: %r" % warning)

    def test_device_macros_with_unsendable_commands_are_dropped(self):
        """機器別マクロも同じ条件で外し、どの機器か知らせること。"""
        manager = self._load([], device_macros=[LIST_WITH_INT, MACRO])

        lab = [g for g in manager.get_groups() if g["name"] == "Lab"]
        self.assertEqual(len(lab), 1, "前提: Lab グループが読めている")
        self.assertEqual(lab[0]["devices"][0]["macros"], [MACRO],
                         "送れない commands のマクロが残っている: %r"
                         % lab[0]["devices"][0]["macros"])
        self.assertIn("R1", manager.load_warning or "",
                      "どの機器のマクロを外したか知らせていない: %r"
                      % manager.load_warning)

    def test_a_macro_without_commands_is_left_alone(self):
        """commands キーの無いマクロ（説明だけ）は落とさないこと。"""
        only_name = {"name": "memo", "description": "あとで書く"}
        manager = self._load([only_name])

        self.assertEqual(manager.get_global_macros(), [only_name],
                         "commands の無いマクロまで外している: %r"
                         % manager.get_global_macros())
        self.assertIsNone(manager.load_warning,
                          "commands の無いマクロで警告が出た: %r"
                          % manager.load_warning)

    def test_good_macros_are_left_alone(self):
        """正しいマクロは、そのまま・警告なしで通すこと。"""
        manager = self._load([MACRO], device_macros=[MACRO])

        self.assertEqual(manager.get_global_macros(), [MACRO])
        self.assertIsNone(manager.load_warning,
                          "正しいマクロで警告が出た: %r" % manager.load_warning)


class MacroCommandsTypeReaderGuardTest(unittest.TestCase):
    """読み手側の保険: 送れない形のマクロは、選べるところに出さないこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_tools_menu_skips_macros_with_unsendable_commands(self):
        """右クリックの「マクロ実行」に、送れない形のマクロを出さないこと。"""
        from PyQt6.QtWidgets import QMenu
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        # 1 秒ごとにシリアルポートを見に行くタイマーを止めておく
        tree._serial_monitor_timer.stop()
        self.addCleanup(tree.close)
        tree.set_tools_state_provider(
            lambda name: {"connected": True, "keepalive_active": False,
                          "command_list_active": False,
                          "macros": BROKEN + [MACRO]})
        menu = QMenu()
        self.addCleanup(menu.deleteLater)

        tree._add_tools_menu(menu, "R1")

        tools = menu.actions()[0].menu()
        macro_menu = [a.menu() for a in tools.actions()
                      if a.text() == "マクロ実行"]
        self.assertEqual(len(macro_menu), 1, "前提: 「マクロ実行」が出ている")
        self.assertEqual([a.text() for a in macro_menu[0].actions()], ["show"],
                         "送れない形のマクロまで項目になっている")

    def test_the_macro_dialog_skips_macros_with_unsendable_commands(self):
        """マクロ設定のプリセット一覧にも出さないこと。"""
        from ui.dialogs.macro_dialog import MacroDialog
        config_manager = mock.Mock()
        config_manager.get_global_macros.return_value = BROKEN + [MACRO]

        dialog = MacroDialog(None, device_name="R1",
                             config_manager=config_manager)
        self.addCleanup(dialog.deleteLater)

        names = [dialog.preset_list_widget.item(i).text()
                 for i in range(dialog.preset_list_widget.count())]
        self.assertEqual(names, ["show"], "送れない形のマクロまで一覧に出ている")

    def test_the_device_dialog_skips_macros_with_unsendable_commands(self):
        """機器編集のマクロ一覧にも出さないこと。"""
        from ui.dialogs.device_dialog import DeviceDialog
        device = _device("R1", macros=BROKEN + [MACRO])

        dialog = DeviceDialog(None, groups=["Lab"], device_data=device)
        self.addCleanup(dialog.deleteLater)

        names = [dialog.macro_list.item(i).text()
                 for i in range(dialog.macro_list.count())]
        self.assertEqual(names, ["show"], "送れない形のマクロまで一覧に出ている")


if __name__ == "__main__":
    unittest.main()
