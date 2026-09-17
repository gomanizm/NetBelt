"""グループの追加・削除で保存だけ失敗したときの案内とツリーの整合。

ConfigManager.add_group / remove_group は save_config() の前に in-memory の
設定を書き換え、保存に失敗しても巻き戻さない。呼び出し側が「失敗しました」と
だけ案内してツリーを放置すると、実行中の設定とツリーが食い違ったまま残り、
次の無関係な保存で、失敗と案内した変更がそのまま永続化される。
グループ編集（改名・自動実行コマンド）と同じ _warn_change_failed へ揃える。
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

    def test_tree_is_refreshed_when_adding_a_group_fails_to_save(self):
        """追加は in-memory に載っているので、ツリーを合わせ直して知らせる。"""
        from unittest import mock
        w = self._window()
        dlg = self._accepted_dialog("保存失敗グループ")
        w.config_manager.save_config = mock.Mock(return_value=False)
        with mock.patch("ui.main_window.GroupDialog", return_value=dlg), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn, \
             mock.patch.object(w, "_load_devices") as reload_tree:
            w._on_add_group()
        warn.assert_called_once()
        reload_tree.assert_called_once()
        self.assertIn("セッション", warn.call_args[0][2])
        # 実行中の設定には載っている（＝ツリーと食い違っていた状態）
        self.assertIsNotNone(w.config_manager.get_group("保存失敗グループ"))

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

    def test_tree_is_refreshed_when_deleting_a_group_fails_to_save(self):
        """削除も in-memory では消えているので、同じ形で知らせる。"""
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
        reload_tree.assert_called_once()
        self.assertIn("セッション", warn.call_args[0][2])
        self.assertIsNone(w.config_manager.get_group("消える予定"))
