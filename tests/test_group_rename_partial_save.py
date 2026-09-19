"""グループの改名だけ保存できたとき、接続先リストが旧名のまま残らないこと。

グループを編集すると、改名（rename_group）と自動実行コマンド
（set_group_auto_commands）で 2 回保存する。1 回目が通って 2 回目だけ
失敗すると（ファイルの一時的なロックなど）、自動実行コマンドを変えて
いなければ「実行中の設定には変更が適用された」とは判定されず、ツリーを
作り直さなかった。

実測: G1（機器 rtr1、自動実行コマンド 'terminal length 0'）を右クリック
→ グループを編集で G2 に改名し、自動実行コマンドは変えずに OK。
save_config の 1 回目だけ本物を通すと、実行中の設定もディスクも G2 なのに
ツリーは G1 のまま。そこから rtr1 を編集すると『機器の更新に失敗しました。』、
削除は『機器の削除に失敗しました。』、ドラッグ移動は『機器の移動に失敗
しました。』になり、ツリーを作り直すまで操作が通らなかった。警告も
『自動実行コマンドの保存に失敗しました。』だけで、改名が保存されたことが
分からなかった。

直し方: 改名が保存できていれば、2 回目が失敗してもツリーを作り直し、
改名は保存したと案内する。1 回の保存にまとめ、失敗したらメモリも戻す形
（update_device と同じ）にすると、保存に失敗した改名を「このセッション中
のみ有効」として残す今の振る舞い（既存テストが守っている）が変わるので
採らない。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class GroupRenamePartialSaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-group-partial-")
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(self.dir))
        ap.start()
        self.addCleanup(ap.stop)

    def _window(self):
        """G1（機器 rtr1、自動実行コマンドあり）を持つメインウィンドウ"""
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = self.dir
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        self.addCleanup(window.close)
        cm = window.config_manager
        self.assertTrue(cm.add_group("G1", ["terminal length 0"]))
        self.assertTrue(cm.add_device("G1", {
            "name": "rtr1", "host": "192.0.2.10", "port": 22,
            "protocol": "ssh", "username": "u", "password": ""}))
        window._load_devices()
        return window

    @staticmethod
    def _groups_in_tree(window):
        root = window.device_tree.tree.invisibleRootItem()
        return [root.child(i).text(0) for i in range(root.childCount())]

    def _rename_with_second_save_failing(self, window, commands):
        """G1 を G2 に改名する。save_config は 1 回目だけ本物を通す"""
        from PyQt6.QtWidgets import QDialog
        cm = window.config_manager
        real_save = cm.save_config
        calls = []

        def flaky_save():
            calls.append(1)
            return real_save() if len(calls) == 1 else False

        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_group_name.return_value = "G2"
        dialog.get_auto_commands.return_value = commands
        with mock.patch("ui.main_window.GroupDialog", return_value=dialog), \
                mock.patch.object(cm, "save_config", side_effect=flaky_save), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_edit_group("G1")
        self.assertEqual(len(calls), 2, "前提: 改名と自動実行コマンドで 2 回保存する")
        return warn

    def test_tree_follows_the_saved_rename_when_the_second_save_fails(self):
        """改名だけ保存できたら、ツリーも新しい名前になること。"""
        window = self._window()
        warn = self._rename_with_second_save_failing(
            window, ["terminal length 0"])   # 自動実行コマンドは変えない

        cm = window.config_manager
        names = [g["name"] for g in cm.get_groups()]
        self.assertIn("G2", names)
        self.assertNotIn("G1", names)
        with open(cm.config_path, encoding="utf-8") as f:
            on_disk = [g["name"] for g in json.load(f)["groups"]]
        self.assertEqual(on_disk, names, "前提: 改名はディスクに保存されている")
        tree = self._groups_in_tree(window)
        self.assertIn("G2", tree, "ツリーが旧名のまま: %s" % tree)
        self.assertNotIn("G1", tree, "ツリーが旧名のまま: %s" % tree)

        warn.assert_called_once()
        message = warn.call_args[0][2]
        self.assertIn("グループ名の変更は保存しました", message)
        self.assertNotIn("セッション", message,
                         "失われる変更は無いのに、終了で失われると案内している")

    def test_device_can_be_edited_from_the_tree_afterwards(self):
        """そのあとツリーの右クリック → 編集で機器を直しても、失敗しないこと。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QDialog
        window = self._window()
        self._rename_with_second_save_failing(window, ["terminal length 0"])

        tree = window.device_tree
        root = tree.tree.invisibleRootItem()
        group = next(root.child(i) for i in range(root.childCount())
                     if root.child(i).text(0) in ("G1", "G2"))
        item = group.child(0)
        device = item.data(0, Qt.ItemDataRole.UserRole)
        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = dict(device, host="192.0.2.11")
        dialog.get_selected_group.return_value = "G2"
        dialog.group_combo.findText.return_value = 0

        def choose_edit(menu, *args, **kwargs):
            # 本物の exec と同じく、選ばれた項目を返す
            return next(a for a in menu.actions() if a.text() == "編集")

        pos = tree.tree.visualItemRect(item).center()
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("PyQt6.QtWidgets.QMenu.exec", choose_edit), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            tree._show_context_menu(pos)
        dialog.exec.assert_called_once()
        warn.assert_not_called()
        self.assertEqual(
            window.config_manager.get_group("G2")["devices"][0]["host"],
            "192.0.2.11")


if __name__ == "__main__":
    unittest.main()
