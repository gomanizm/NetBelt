"""グループの自動実行コマンド（auto_commands）の保存と編集 UI。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


class SetGroupAutoCommandsTest(unittest.TestCase):
    """ConfigManager.set_group_auto_commands の振る舞い。"""

    def _new_manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-autocmd-")
        return ConfigManager(config_path=os.path.join(d, "config.json")), d

    def test_roundtrip_through_file(self):
        cm, d = self._new_manager()
        self.assertTrue(cm.add_group("検証環境A"))
        self.assertTrue(
            cm.set_group_auto_commands("検証環境A", ["terminal length 0", "show clock"]))

        from core.config_manager import ConfigManager
        cm2 = ConfigManager(config_path=os.path.join(d, "config.json"))
        self.assertEqual(
            cm2.get_group("検証環境A")["auto_commands"],
            ["terminal length 0", "show clock"])

    def test_empty_list_is_allowed(self):
        cm, _ = self._new_manager()
        cm.add_group("空グループ", ["show version"])
        self.assertTrue(cm.set_group_auto_commands("空グループ", []))
        self.assertEqual(cm.get_group("空グループ")["auto_commands"], [])

    def test_unknown_group_returns_false(self):
        cm, _ = self._new_manager()
        self.assertFalse(cm.set_group_auto_commands("存在しない", ["show clock"]))

    def test_does_not_depend_on_get_group_returning_a_reference(self):
        """get_group がコピーを返すようになっても壊れないこと。"""
        import copy
        cm, _ = self._new_manager()
        cm.add_group("参照非依存")
        original_get_group = cm.get_group
        cm.get_group = lambda name: copy.deepcopy(original_get_group(name))
        self.assertTrue(cm.set_group_auto_commands("参照非依存", ["show ip int br"]))
        cm.get_group = original_get_group
        self.assertEqual(cm.get_group("参照非依存")["auto_commands"], ["show ip int br"])

    def test_stored_commands_are_a_copy(self):
        """呼び出し側が渡したリストを後から変更しても保存済みの値が変わらないこと。"""
        cm, _ = self._new_manager()
        cm.add_group("コピー確認")
        commands = ["show clock"]
        cm.set_group_auto_commands("コピー確認", commands)
        commands.append("reload")
        self.assertEqual(cm.get_group("コピー確認")["auto_commands"], ["show clock"])

    def test_rename_group_keeps_auto_commands(self):
        cm, _ = self._new_manager()
        cm.add_group("旧名", ["terminal monitor"])
        self.assertTrue(cm.rename_group("旧名", "新名"))
        self.assertEqual(cm.get_group("新名")["auto_commands"], ["terminal monitor"])


class GroupDialogAutoCommandsTest(unittest.TestCase):
    """GroupDialog の自動実行コマンド入力欄。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_existing_commands_are_shown_one_per_line(self):
        from ui.dialogs.group_dialog import GroupDialog
        dlg = GroupDialog(None, group_name="本番環境",
                          auto_commands=["terminal monitor", "terminal length 0"])
        self.assertEqual(dlg.auto_commands_edit.toPlainText(),
                         "terminal monitor\nterminal length 0")

    def test_getter_strips_and_drops_blank_lines(self):
        from ui.dialogs.group_dialog import GroupDialog
        dlg = GroupDialog(None)
        dlg.auto_commands_edit.setPlainText("  show clock  \n\n\tshow version\n   \n")
        self.assertEqual(dlg.get_auto_commands(), ["show clock", "show version"])

    def test_empty_is_allowed(self):
        """auto_commands は空が正常状態なので、空欄で OK を押せること。"""
        from ui.dialogs.group_dialog import GroupDialog
        dlg = GroupDialog(None)
        dlg.name_edit.setText("空でも通る")
        dlg.auto_commands_edit.setPlainText("")
        dlg._on_ok()
        self.assertEqual(dlg.result(), int(dlg.DialogCode.Accepted))
        self.assertEqual(dlg.get_auto_commands(), [])

    def test_omitting_auto_commands_defaults_to_empty(self):
        """None が空リストへ正規化されること（正規化を怠ると _load_data で落ちる）。"""
        from ui.dialogs.group_dialog import GroupDialog
        dlg = GroupDialog(None, group_name="既存")
        self.assertEqual(dlg.auto_commands, [])
        self.assertEqual(dlg.auto_commands_edit.toPlainText(), "")
        self.assertEqual(dlg.get_auto_commands(), [])

    def test_labels_wrap_so_the_dialog_keeps_its_width(self):
        """折り返さないラベルがあるとテキスト全長が最小幅になり resize() が効かない。"""
        from ui.dialogs.group_dialog import GroupDialog
        dlg = GroupDialog(None)
        self.assertTrue(dlg.auto_commands_help_label.wordWrap())
        self.assertTrue(dlg.auto_commands_warning_label.wordWrap())

        # 絶対値のしきい値はフォント・DPI・Qt スタイルで揺れるため、
        # 「折り返さなかった場合に必要な幅」と相対比較する。
        # 折り返しが効いていなければ最小幅はこの値以上になる。
        help_label = dlg.auto_commands_help_label
        unwrapped_width = help_label.fontMetrics().boundingRect(
            help_label.text()).width()
        self.assertLess(dlg.minimumSizeHint().width(), unwrapped_width)

    def test_plaintext_warning_is_shown(self):
        """auto_commands は暗号化されないので、注意書きを消してはいけない。"""
        from ui.dialogs.group_dialog import GroupDialog
        dlg = GroupDialog(None)
        self.assertIn("平文", dlg.auto_commands_warning_label.text())


class GroupEditWiringTest(unittest.TestCase):
    """main_window のグループ追加/編集がコマンドを保存すること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from unittest import mock
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-groupwire-")
        # 起動時の更新チェックは実際に GitHub API を叩くのでモックする
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(config_path=os.path.join(d, "config.json"))
            return MainWindow()

    def test_add_group_passes_auto_commands(self):
        from unittest import mock
        from PyQt6.QtWidgets import QDialog
        w = self._window()
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.get_group_name.return_value = "新グループ"
        dlg.get_auto_commands.return_value = ["terminal length 0"]
        with mock.patch("ui.main_window.GroupDialog", return_value=dlg):
            w._on_add_group()
        self.assertEqual(
            w.config_manager.get_group("新グループ")["auto_commands"],
            ["terminal length 0"])

    def test_edit_group_saves_auto_commands_under_the_new_name(self):
        from unittest import mock
        from PyQt6.QtWidgets import QDialog
        w = self._window()
        w.config_manager.add_group("編集前", ["show version"])
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.get_group_name.return_value = "編集後"
        dlg.get_auto_commands.return_value = ["show clock"]
        with mock.patch("ui.main_window.GroupDialog", return_value=dlg):
            w._on_edit_group("編集前")
        self.assertIsNone(w.config_manager.get_group("編集前"))
        self.assertEqual(
            w.config_manager.get_group("編集後")["auto_commands"], ["show clock"])

    def test_editing_only_commands_does_not_warn(self):
        """名前を変えずに OK を押しても警告が出ないこと（既存バグの回帰防止）。"""
        from unittest import mock
        from PyQt6.QtWidgets import QDialog
        w = self._window()
        w.config_manager.add_group("同じ名前", [])
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.get_group_name.return_value = "同じ名前"
        dlg.get_auto_commands.return_value = ["terminal length 0"]
        with mock.patch("ui.main_window.GroupDialog", return_value=dlg), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn:
            w._on_edit_group("同じ名前")
        warn.assert_not_called()
        self.assertEqual(
            w.config_manager.get_group("同じ名前")["auto_commands"],
            ["terminal length 0"])

    def test_edit_group_passes_existing_commands_to_the_dialog(self):
        from unittest import mock
        from PyQt6.QtWidgets import QDialog
        w = self._window()
        w.config_manager.add_group("既存コマンドあり", ["terminal monitor"])
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.DialogCode.Rejected
        with mock.patch("ui.main_window.GroupDialog", return_value=dlg) as ctor:
            w._on_edit_group("既存コマンドあり")
        self.assertEqual(ctor.call_args.kwargs["auto_commands"], ["terminal monitor"])


if __name__ == "__main__":
    unittest.main()
