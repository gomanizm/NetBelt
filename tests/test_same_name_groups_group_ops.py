"""同じ名前のグループが 2 つあるとき、後ろの方から先に並んでいる方を書き換えないことを検証する。

実測（ebbe593）: 手編集の config.json に kyoten を 2 つ置く（1 つ目の自動実行
コマンドは show clock、2 つ目は terminal length 0）。接続先リストで 2 つ目の
kyoten を右クリックすると、
  グループを編集: ダイアログには 1 つ目の自動実行コマンド（show clock）が出て、
        show version に変えて OK すると 1 つ目の kyoten の自動実行コマンドが
        書き換わる。2 つ目は変わらない。1 つ目の機器へ次に繋いだとき、2 つ目の
        つもりで入れたコマンドが送られる。
  グループを削除: 1 つ目が空で 2 つ目に機器があると、「機器が含まれています」
        と断らずに、右クリックしていない 1 つ目（空）を消す。
  ドロップ: other の機器 X を 2 つ目の kyoten へ落とすと、X は 1 つ目の
        kyoten に入る（1 つ目の自動実行コマンドが X へ送られる）。
グループの編集・削除とドロップ先は、グループ名だけで MainWindow へ渡り、
ConfigManager が get_group(グループ名) で先頭のグループを引くため。

直し方: 接続先リスト（DeviceTree）が、先に同じ名前のグループが並んでいる
グループ項目では、グループの編集・削除を灰色にして選べなくし、そこへの
ドロップを断る（何も変えない）。先に並んでいる方と、同じ名前の無いグループは
今までどおり。重複は、先に並んでいる方を「グループを編集」で別の名前にすれば
解ける（読み込み時の案内もそちらを指す）。設定ファイルのグループ名は変えない。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _device(name, host):
    return {"name": name, "host": host, "port": 22, "protocol": "ssh",
            "username": "admin", "password": "", "ssh_key": "", "macros": []}


def _group(name, devices, auto_commands=()):
    return {"name": name, "auto_commands": list(auto_commands),
            "devices": list(devices)}


def _groups():
    return [
        _group("Default", []),
        _group("kyoten", [], ["show clock"]),
        _group("other", [_device("X", "192.0.2.9")]),
        _group("kyoten", [_device("R", "192.0.2.2")], ["terminal length 0"]),
    ]


class SameNameGroupsGroupOpsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 破棄済みウィジェットへのシグナル配送で落ちるため保持する
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-dupgrpmenu-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "config.json"

    def _window(self, groups=None):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        self.path.write_text(json.dumps({
            "config_version": "1.0",
            "groups": groups or _groups(),
            "global_macros": [],
            "settings": {},
        }), encoding="utf-8")
        cm = ConfigManager(config_path=str(self.path))
        with mock.patch("ui.main_window.ConfigManager", return_value=cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        type(self)._windows.append(window)
        self.before = self._on_disk()
        return window

    def _on_disk(self):
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [(g["name"], g.get("auto_commands"),
                 [(d["name"], d["host"]) for d in g["devices"]])
                for g in raw["groups"]]

    @staticmethod
    def _group_item(window, name, nth):
        tree = window.device_tree.tree
        items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        return [item for item in items if item.text(0) == name][nth]

    def _choose_from_group_menu(self, window, group_item, text):
        """グループ項目を右クリックし、text の項目を選ぶ（選べなければ何も選ばない）。

        選べる項目の文言を返す。
        """
        from PyQt6.QtWidgets import QDialog, QMessageBox
        tree = window.device_tree
        offered = []

        def fake_exec(menu, *args, **kwargs):
            chosen = None
            for action in menu.actions():
                if action.isEnabled() and action.text():
                    offered.append(action.text())
                    if action.text() == text:
                        chosen = action
            return chosen

        dialog = mock.MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_group_name.return_value = "kyoten"
        dialog.get_auto_commands.return_value = ["show version"]
        with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec), \
                mock.patch("ui.main_window.GroupDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.question",
                           return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            tree._show_group_menu(
                tree.tree.visualItemRect(group_item).center(), group_item)
        return offered

    def _drop(self, window, source_item, target_item):
        tree = window.device_tree
        tree.tree.setCurrentItem(source_item)
        event = mock.Mock()
        with mock.patch.object(tree.tree, "itemAt", return_value=target_item), \
                mock.patch.object(tree.tree, "dropIndicatorPosition",
                                  return_value=None), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            tree._on_drop_event(event)
        return event

    def test_editing_the_later_group_does_not_change_the_first(self):
        window = self._window()

        offered = self._choose_from_group_menu(
            window, self._group_item(window, "kyoten", 1), "グループを編集")

        self.assertEqual(self._on_disk(), self.before,
                         "後ろの kyoten の編集で、1 つ目の kyoten が書き換わった")
        self.assertNotIn("グループを編集", offered,
                         "後ろの kyoten でもグループの編集を選べる")

    def test_deleting_the_later_group_does_not_remove_the_first(self):
        window = self._window()

        offered = self._choose_from_group_menu(
            window, self._group_item(window, "kyoten", 1), "グループを削除")

        self.assertEqual(self._on_disk(), self.before,
                         "後ろの kyoten の削除で、1 つ目の kyoten が消えた")
        self.assertNotIn("グループを削除", offered,
                         "後ろの kyoten でもグループの削除を選べる")

    def test_the_later_group_items_are_shown_but_greyed(self):
        """選べないことが分かるよう、項目は灰色で出すこと（非表示は選べる）。"""
        window = self._window()
        group_item = self._group_item(window, "kyoten", 1)
        tree = window.device_tree
        shown = []

        def fake_exec(menu, *args, **kwargs):
            shown.extend((a.text(), a.isEnabled()) for a in menu.actions()
                         if a.text())
            return None

        with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
            tree._show_group_menu(
                tree.tree.visualItemRect(group_item).center(), group_item)

        self.assertIn(("グループを編集", False), shown)
        self.assertIn(("グループを削除", False), shown)
        self.assertIn(("接続先リストを非表示", True), shown)

    def test_the_first_group_can_still_be_edited(self):
        """先に並んでいる方は今までどおり編集できること（重複を解く道）。"""
        window = self._window()

        offered = self._choose_from_group_menu(
            window, self._group_item(window, "kyoten", 0), "グループを編集")

        self.assertIn("グループを編集", offered)
        self.assertEqual(self._on_disk()[1],
                         ("kyoten", ["show version"], []),
                         "先に並んでいる kyoten を編集できない")
        self.assertEqual(self._on_disk()[3], self.before[3])

    def test_the_first_group_can_still_be_deleted(self):
        window = self._window()

        offered = self._choose_from_group_menu(
            window, self._group_item(window, "kyoten", 0), "グループを削除")

        self.assertIn("グループを削除", offered)
        self.assertEqual([name for name, _, _ in self._on_disk()],
                         ["Default", "other", "kyoten"])
        self.assertEqual(self._on_disk()[2], self.before[3],
                         "後ろの kyoten が変わった")

    def test_a_group_without_a_same_named_one_is_unaffected(self):
        window = self._window()

        offered = self._choose_from_group_menu(
            window, self._group_item(window, "other", 0), "")

        self.assertIn("グループを編集", offered)
        self.assertIn("グループを削除", offered)

    def test_dropping_onto_the_later_group_changes_nothing(self):
        window = self._window()
        x_item = self._group_item(window, "other", 0).child(0)

        event = self._drop(window, x_item, self._group_item(window, "kyoten", 1))

        self.assertEqual(self._on_disk(), self.before,
                         "後ろの kyoten へ落とした機器が、1 つ目の kyoten に入った")
        self.assertFalse(event.accept.called, "後ろの kyoten へのドロップを受け付けた")

    def test_dropping_onto_a_device_of_the_later_group_changes_nothing(self):
        window = self._window()
        x_item = self._group_item(window, "other", 0).child(0)
        r_item = self._group_item(window, "kyoten", 1).child(0)

        event = self._drop(window, x_item, r_item)

        self.assertEqual(self._on_disk(), self.before)
        self.assertFalse(event.accept.called)

    def test_dropping_onto_the_first_group_still_moves(self):
        window = self._window()
        x_item = self._group_item(window, "other", 0).child(0)

        event = self._drop(window, x_item, self._group_item(window, "kyoten", 0))

        self.assertTrue(event.accept.called)
        self.assertEqual(self._on_disk()[1],
                         ("kyoten", ["show clock"], [("X", "192.0.2.9")]))
        self.assertEqual(self._on_disk()[3], self.before[3])

    def test_the_notice_points_at_the_first_group(self):
        """読み込み時の案内が、先に並んでいる方の「グループを編集」を指すこと。"""
        window = self._window()

        notice = window.config_manager.load_warning
        self.assertIn("先に並んでいる方を右クリック", notice)
        self.assertNotIn("どちらかのグループを右クリック", notice,
                         "後ろの方からは編集できないのに、どちらからでもと案内している")


if __name__ == "__main__":
    unittest.main()
