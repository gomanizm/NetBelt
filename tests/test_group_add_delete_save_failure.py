"""グループの追加・削除で保存だけ失敗したときの案内とツリーの整合。

ConfigManager.add_group / remove_group は save_config() の前に in-memory の
設定を書き換える。以前は保存に失敗しても巻き戻さず、「このセッション中のみ
有効」と案内していたが、終了時のレイアウト保存がそのメモリを書き出すので、
案内と逆に変更が残っていた。2026-09-20 の決定で、機器・マクロと同じく
保存に失敗したらメモリも戻し、「反映していません」と伝える形に変えた。
巻き戻すのでツリーは作り直さない。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


class GroupAddDeleteSaveFailureTest(unittest.TestCase):
    """_on_add_group / _on_delete_group の保存失敗時の振る舞い。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from unittest import mock
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-groupsave-")
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

    def test_a_failed_add_is_rolled_back(self):
        """保存できなかった追加は in-memory からも消す。ツリーはそのまま。"""
        from unittest import mock
        w = self._window()
        dlg = self._accepted_dialog("保存失敗グループ")
        w.config_manager.save_config = mock.Mock(return_value=False)
        with mock.patch("ui.main_window.GroupDialog", return_value=dlg), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn, \
             mock.patch.object(w, "_load_devices") as reload_tree:
            w._on_add_group()
        warn.assert_called_once()
        reload_tree.assert_not_called()
        self.assertIn("反映していません", warn.call_args[0][2])
        self.assertIsNone(w.config_manager.get_group("保存失敗グループ"))

    def test_duplicate_group_is_not_reported_as_a_save_failure(self):
        """add_group は同名でも False。保存失敗と混同した案内をしないこと。"""
        from unittest import mock
        w = self._window()
        w.config_manager.add_group("既にある", [])
        dlg = self._accepted_dialog("既にある")
        with mock.patch("ui.main_window.GroupDialog", return_value=dlg), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn:
            w._on_add_group()
        message = warn.call_args[0][2]
        self.assertNotIn("セッション", message)
        self.assertIn("失敗", message)

    def test_a_failed_delete_is_rolled_back(self):
        """削除も同じ。保存できなければ in-memory へ戻す。"""
        from unittest import mock
        from PyQt6.QtWidgets import QMessageBox
        w = self._window()
        w.config_manager.add_group("消える予定", [])
        w.config_manager.save_config = mock.Mock(return_value=False)
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn, \
             mock.patch.object(w, "_load_devices") as reload_tree:
            w._on_delete_group("消える予定")
        warn.assert_called_once()
        reload_tree.assert_not_called()
        self.assertIn("反映していません", warn.call_args[0][2])
        self.assertIsNotNone(w.config_manager.get_group("消える予定"))
