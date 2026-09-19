"""設定が何も変わっていない失敗で、ツリーの状態を捨てないこと。

_warn_change_failed は引数に関係なく _load_devices() を呼んでいた。
「同名グループが既にある」だけの拒否では実行中の設定は何も変わって
いないのに、ツリーが作り直されて畳んでいたグループが開き直り、選択も
外れる。2026-09-20 の決定でグループの変更も保存失敗時に巻き戻すように
なり、どちらの失敗でも実行中の設定は変わらないので、作り直さない。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


class ChangeFailureKeepsTreeStateTest(unittest.TestCase):
    """_warn_change_failed のツリー再読み込み条件。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from unittest import mock
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-treestate-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            return MainWindow()

    def _accepted_dialog(self, group_name, auto_commands=None):
        from unittest import mock
        from PyQt6.QtWidgets import QDialog
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.get_group_name.return_value = group_name
        dlg.get_auto_commands.return_value = auto_commands or []
        return dlg

    def _find_group_item(self, tree, group_name):
        """ツリーの最上位から、その名前のグループ項目を探す。"""
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item.text(0).split()[0] == group_name:
                return item
        self.fail("グループ '%s' がツリーに見つからない" % group_name)

    def test_a_rejected_change_does_not_reload_the_tree(self):
        """受け付けられなかった変更では、ツリーはそのまま。"""
        from unittest import mock
        w = self._window()
        with mock.patch("ui.main_window.QMessageBox.warning") as warn, \
             mock.patch.object(w, "_load_devices") as reload_tree:
            w._warn_change_failed("グループの追加", False)
        warn.assert_called_once()
        self.assertNotIn("反映していません", warn.call_args[0][2])
        reload_tree.assert_not_called()

    def test_a_save_failure_does_not_reload_the_tree_either(self):
        """保存だけ失敗した分も in-memory ごと巻き戻るので、作り直さない。

        以前は in-memory に変更が残っていたので合わせ直していた
        （2026-09-20 の決定で巻き戻しに変更）。
        """
        from unittest import mock
        w = self._window()
        with mock.patch("ui.main_window.QMessageBox.warning") as warn, \
             mock.patch.object(w, "_load_devices") as reload_tree:
            w._warn_change_failed("グループの追加", True)
        warn.assert_called_once()
        self.assertIn("反映していません", warn.call_args[0][2])
        reload_tree.assert_not_called()

    def test_duplicate_group_add_keeps_expansion_and_selection(self):
        """同名グループの追加を断られても、畳み具合と選択が残ること。"""
        from unittest import mock
        w = self._window()
        w.config_manager.add_group("GroupA", [])
        w.config_manager.add_device("GroupA", {
            "name": "R1", "host": "192.0.2.1", "port": 22,
            "connection_type": "SSH", "username": "admin",
        })
        w._load_devices()

        tree = w.device_tree.tree
        group_item = self._find_group_item(tree, "GroupA")
        group_item.setExpanded(False)
        device_item = group_item.child(0)
        tree.setCurrentItem(device_item)
        self.assertFalse(group_item.isExpanded())
        self.assertIsNotNone(tree.currentItem())

        dlg = self._accepted_dialog("GroupA")
        with mock.patch("ui.main_window.GroupDialog", return_value=dlg), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn:
            w._on_add_group()
        warn.assert_called_once()

        after_group = self._find_group_item(w.device_tree.tree, "GroupA")
        self.assertIs(after_group, group_item)
        self.assertFalse(after_group.isExpanded())
        self.assertIs(w.device_tree.tree.currentItem(), device_item)


if __name__ == "__main__":
    unittest.main()
