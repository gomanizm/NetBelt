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


if __name__ == "__main__":
    unittest.main()
