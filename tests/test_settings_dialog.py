"""設定ダイアログ（ツール→設定）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


class SettingsDialogTerminalTabTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-settingsdlg-")
        return ConfigManager(config_path=os.path.join(d, "config.json"))

    def test_dialog_has_three_tabs(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        dlg = SettingsDialog(None, config_manager=self._manager())
        titles = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
        self.assertEqual(titles, ["ターミナル", "SFTPクライアント", "更新"])

    def test_terminal_values_are_restored_from_config(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        cm.set_server_settings("terminal", {
            "background_color": "#112233", "text_color": "#445566",
            "font_family": "Courier New", "font_size": 13})
        dlg = SettingsDialog(None, config_manager=cm)
        self.assertEqual(dlg._background_color, "#112233")
        self.assertEqual(dlg._text_color, "#445566")
        self.assertEqual(dlg.font_combo.currentFont().family(), "Courier New")
        self.assertEqual(dlg.font_size_spin.value(), 13)

    def test_terminal_values_are_saved(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        dlg = SettingsDialog(None, config_manager=cm)
        dlg._background_color = "#0A0B0C"
        dlg._text_color = "#FAFBFC"
        dlg.font_size_spin.setValue(17)
        self.assertTrue(dlg.save_settings())

        saved = cm.get_server_settings("terminal")
        self.assertEqual(saved["background_color"], "#0A0B0C")
        self.assertEqual(saved["text_color"], "#FAFBFC")
        self.assertEqual(saved["font_size"], 17)

    def test_saving_does_not_wipe_other_sections(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        cm.config.setdefault("settings", {})["ui_layout"] = {"tool_tab": 3}
        dlg = SettingsDialog(None, config_manager=cm)
        dlg.save_settings()
        self.assertEqual(cm.config["settings"]["ui_layout"], {"tool_tab": 3})

    def test_font_size_spin_range(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        dlg = SettingsDialog(None, config_manager=self._manager())
        self.assertEqual(dlg.font_size_spin.minimum(), 6)
        self.assertEqual(dlg.font_size_spin.maximum(), 32)

    def test_missing_keys_fall_back_to_defaults(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        from ui.terminal_widget import TerminalWidget
        cm = self._manager()
        cm.config["settings"]["terminal"] = {}
        dlg = SettingsDialog(None, config_manager=cm)
        self.assertEqual(dlg._background_color,
                         TerminalWidget.DEFAULT_TERMINAL_SETTINGS["background_color"])
        self.assertEqual(dlg.font_size_spin.value(),
                         TerminalWidget.DEFAULT_TERMINAL_SETTINGS["font_size"])

    def test_broken_config_values_are_shown_normalized(self):
        """壊れた値をそのまま画面へ出さないこと。

        生値を出すとターミナルの見た目と食い違ううえ、何も変えずに OK を
        押しただけで壊れた値を書き戻してしまう。
        """
        from ui.terminal_widget import TerminalWidget
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        cm.set_server_settings("terminal", {
            "background_color": "not-a-color", "font_size": 1000})
        defaults = TerminalWidget.DEFAULT_TERMINAL_SETTINGS

        dlg = SettingsDialog(None, config_manager=cm)
        self.assertEqual(dlg._background_color, defaults["background_color"])
        self.assertEqual(dlg.font_size_spin.value(), defaults["font_size"])

    def test_opening_and_accepting_does_not_write_back_broken_values(self):
        """設定を見に開いて OK を押しただけで壊れた値が残らないこと。"""
        from ui.terminal_widget import TerminalWidget
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        cm.set_server_settings("terminal", {
            "background_color": "not-a-color", "font_size": 1000})
        defaults = TerminalWidget.DEFAULT_TERMINAL_SETTINGS

        SettingsDialog(None, config_manager=cm).save_settings()

        saved = cm.get_server_settings("terminal")
        self.assertEqual(saved["background_color"], defaults["background_color"])
        self.assertEqual(saved["font_size"], defaults["font_size"])

    def test_failed_save_is_reported(self):
        """保存できなかったことを黙らないこと。"""
        from unittest import mock
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        cm.save_config = mock.Mock(return_value=False)
        dlg = SettingsDialog(None, config_manager=cm)
        with mock.patch("ui.dialogs.settings_dialog.QMessageBox.warning") as warn:
            dlg._on_ok()
        warn.assert_called_once()
        self.assertNotEqual(dlg.result(), int(dlg.DialogCode.Accepted))


class SettingsDialogUpdateTabTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-settingsupd-")
        return ConfigManager(config_path=os.path.join(d, "config.json"))

    def test_check_on_startup_is_restored_and_saved(self):
        """両方向を確かめる。

        QCheckBox の既定は未チェックなので、False だけを確認しても
        復元経路を消したことに気づけない。
        """
        from ui.dialogs.settings_dialog import SettingsDialog
        for enabled in (True, False):
            with self.subTest(check_on_startup=enabled):
                cm = self._manager()
                cm.set_check_on_startup(enabled)
                dlg = SettingsDialog(None, config_manager=cm)
                self.assertEqual(dlg.check_on_startup_box.isChecked(), enabled)

        cm = self._manager()
        cm.set_check_on_startup(False)
        dlg = SettingsDialog(None, config_manager=cm)
        dlg.check_on_startup_box.setChecked(True)
        dlg.save_settings()
        self.assertTrue(cm.get_check_on_startup())

    def test_skipped_version_is_shown_when_present(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        cm.set_skipped_version("1.2.3")
        dlg = SettingsDialog(None, config_manager=cm)
        # offscreen では isVisible() が常に False なので、有効/無効で見る
        # （スキップが無いときは test_no_skip_shows_a_placeholder が False を見る）
        self.assertTrue(dlg.clear_skip_button.isEnabled())
        self.assertIn("1.2.3", dlg.skipped_version_label.text())

    def test_skip_can_be_cleared(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        cm.set_skipped_version("1.2.3")
        dlg = SettingsDialog(None, config_manager=cm)
        dlg._on_clear_skip()
        dlg.save_settings()
        self.assertIsNone(cm.get_skipped_version())

    def test_no_skip_shows_a_placeholder(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        dlg = SettingsDialog(None, config_manager=cm)
        self.assertFalse(dlg.clear_skip_button.isEnabled())
        self.assertIn("ありません", dlg.skipped_version_label.text())


    def test_clearing_the_skip_does_not_leak_when_cancelled(self):
        """解除は save_settings で初めて効くこと（2段構えにしている理由）。"""
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        cm.set_skipped_version("1.2.3")
        dlg = SettingsDialog(None, config_manager=cm)
        dlg._on_clear_skip()
        dlg.reject()
        self.assertEqual(cm.get_skipped_version(), "1.2.3")

    def test_failed_update_save_is_not_reported_as_success(self):
        """更新設定だけ保存に失敗しても成功扱いにしないこと。"""
        from unittest import mock
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        dlg = SettingsDialog(None, config_manager=cm)
        cm.set_check_on_startup = mock.Mock(return_value=False)
        self.assertFalse(dlg.save_settings())
    def test_github_token_is_not_exposed(self):
        """平文で残るのでダイアログには出さない。"""
        from ui.dialogs.settings_dialog import SettingsDialog
        dlg = SettingsDialog(None, config_manager=self._manager())
        self.assertFalse(hasattr(dlg, "github_token_edit"))


class SettingsDialogSftpTabTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-settingssftp-")
        return ConfigManager(config_path=os.path.join(d, "config.json"))

    def test_sftp_values_are_restored(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        cm.set_server_settings("sftp", {
            "default_download_path": "D:/受信", "show_hidden_files": True,
            "confirm_delete": False, "confirm_overwrite": False})
        dlg = SettingsDialog(None, config_manager=cm)
        self.assertEqual(dlg.download_path_edit.text(), "D:/受信")
        self.assertTrue(dlg.show_hidden_box.isChecked())
        self.assertFalse(dlg.confirm_delete_box.isChecked())
        self.assertFalse(dlg.confirm_overwrite_box.isChecked())

    def test_sftp_values_are_saved(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        dlg = SettingsDialog(None, config_manager=cm)
        dlg.download_path_edit.setText("E:/保存先")
        dlg.show_hidden_box.setChecked(True)
        dlg.confirm_delete_box.setChecked(False)
        dlg.save_settings()

        saved = cm.get_server_settings("sftp")
        self.assertEqual(saved["default_download_path"], "E:/保存先")
        self.assertTrue(saved["show_hidden_files"])
        self.assertFalse(saved["confirm_delete"])


    def test_confirm_overwrite_is_restored_and_saved(self):
        """復元も保存もテストが無かったキー。両方向で確かめる。"""
        from ui.dialogs.settings_dialog import SettingsDialog
        for enabled in (True, False):
            with self.subTest(confirm_overwrite=enabled):
                cm = self._manager()
                cm.set_server_settings("sftp", {"confirm_overwrite": enabled})
                dlg = SettingsDialog(None, config_manager=cm)
                self.assertEqual(dlg.confirm_overwrite_box.isChecked(), enabled)

                dlg.confirm_overwrite_box.setChecked(not enabled)
                dlg.save_settings()
                self.assertEqual(
                    cm.get_server_settings("sftp")["confirm_overwrite"], not enabled)

    def test_broken_sftp_values_do_not_break_the_dialog(self):
        """壊れた値でダイアログが開けなくなっていた（setChecked に null が渡る）。"""
        from ui.sftp_panel import SFTPPanel
        from ui.dialogs.settings_dialog import SettingsDialog
        defaults = SFTPPanel.SFTP_SETTING_DEFAULTS
        cm = self._manager()
        cm.set_server_settings("sftp", {
            "show_hidden_files": None, "confirm_delete": "yes",
            "default_download_path": 123})

        dlg = SettingsDialog(None, config_manager=cm)
        self.assertEqual(dlg.show_hidden_box.isChecked(),
                         defaults["show_hidden_files"])
        self.assertEqual(dlg.confirm_delete_box.isChecked(),
                         defaults["confirm_delete"])
        self.assertEqual(dlg.download_path_edit.text(),
                         defaults["default_download_path"])
    def test_sftp_defaults_match_the_panel(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        from ui.sftp_panel import SFTPPanel
        cm = self._manager()
        cm.config["settings"]["sftp"] = {}
        dlg = SettingsDialog(None, config_manager=cm)
        self.assertEqual(dlg.download_path_edit.text(),
                         SFTPPanel.SFTP_SETTING_DEFAULTS["default_download_path"])
        self.assertEqual(dlg.confirm_delete_box.isChecked(),
                         SFTPPanel.SFTP_SETTING_DEFAULTS["confirm_delete"])

    def test_failed_sftp_save_is_not_reported_as_success(self):
        """SFTP設定だけ保存に失敗しても成功扱いにしないこと。

        terminal タブと同じ set_server_settings を使うため、sftp 側の
        戻り値だけを False にして terminal 側とは切り分けて確かめる。
        """
        from unittest import mock
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        dlg = SettingsDialog(None, config_manager=cm)
        cm.set_server_settings = mock.Mock(
            side_effect=lambda key, values: key != "sftp")
        self.assertFalse(dlg.save_settings())


if __name__ == "__main__":
    unittest.main()
