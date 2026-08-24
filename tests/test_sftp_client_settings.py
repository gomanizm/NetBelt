"""SFTP クライアント設定（settings.sftp）が実際に効くこと。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpClientSettingsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self, sftp_settings=None):
        from core.config_manager import ConfigManager
        from ui.sftp_panel import SFTPPanel
        d = tempfile.mkdtemp(prefix="netbelt-sftpset-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        if sftp_settings is not None:
            cm.set_server_settings("sftp", sftp_settings)
        panel = SFTPPanel(config_manager=cm)
        panel.sftp_manager = mock.Mock()
        panel.sftp_manager.get_current_path.return_value = "/home/admin"
        return panel, cm

    def _entry(self, name, is_dir=False):
        return {"name": name, "is_dir": is_dir, "size": 10,
                "permissions": "-rw-r--r--", "mtime": 0}

    def test_constructor_accepts_config_manager(self):
        panel, cm = self._panel()
        self.assertIs(panel.config_manager, cm)

    def test_hidden_files_are_filtered_by_default(self):
        panel, _ = self._panel()
        panel._update_file_list([self._entry("visible.txt"), self._entry(".hidden")])
        self.assertEqual(panel.model.rowCount(), 1)

    def test_hidden_files_are_shown_when_enabled(self):
        panel, _ = self._panel({"show_hidden_files": True})
        panel._update_file_list([self._entry("visible.txt"), self._entry(".hidden")])
        self.assertEqual(panel.model.rowCount(), 2)

    def test_delete_skips_confirmation_when_disabled(self):
        panel, _ = self._panel({"confirm_delete": False})
        with mock.patch("ui.sftp_panel.QMessageBox.question") as question:
            panel._on_delete_selected(self._entry("消す.txt"))
        question.assert_not_called()
        panel.sftp_manager.delete_item.assert_called_once()

    def test_delete_confirms_by_default(self):
        from PyQt6.QtWidgets import QMessageBox
        panel, _ = self._panel()
        with mock.patch("ui.sftp_panel.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.No) as question:
            panel._on_delete_selected(self._entry("消す.txt"))
        question.assert_called_once()
        panel.sftp_manager.delete_item.assert_not_called()

    def test_download_uses_the_default_path(self):
        panel, _ = self._panel({"default_download_path": "D:/受信"})
        with mock.patch("ui.sftp_panel.QFileDialog.getSaveFileName",
                        return_value=("", "")) as save_dialog:
            panel._on_download_selected(self._entry("config.txt"))
        suggested = save_dialog.call_args[0][2]
        self.assertTrue(suggested.replace("\\", "/").startswith("D:/受信"))
        self.assertTrue(suggested.endswith("config.txt"))

    def test_upload_confirms_overwrite_when_the_name_exists(self):
        from PyQt6.QtWidgets import QMessageBox
        panel, _ = self._panel()
        panel._update_file_list([self._entry("既存.cfg")])
        with mock.patch("ui.sftp_panel.QFileDialog.getOpenFileName",
                        return_value=("C:/tmp/既存.cfg", "")), \
             mock.patch("ui.sftp_panel.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.No) as question:
            panel._on_upload()
        question.assert_called_once()
        panel.sftp_manager.upload_file.assert_not_called()

    def test_upload_does_not_confirm_for_a_new_name(self):
        panel, _ = self._panel()
        panel._update_file_list([self._entry("別名.cfg")])
        with mock.patch("ui.sftp_panel.QFileDialog.getOpenFileName",
                        return_value=("C:/tmp/新規.cfg", "")), \
             mock.patch("ui.sftp_panel.QMessageBox.question") as question:
            panel._on_upload()
        question.assert_not_called()
        panel.sftp_manager.upload_file.assert_called_once()

    def test_upload_skips_confirmation_when_disabled(self):
        panel, _ = self._panel({"confirm_overwrite": False})
        panel._update_file_list([self._entry("既存.cfg")])
        with mock.patch("ui.sftp_panel.QFileDialog.getOpenFileName",
                        return_value=("C:/tmp/既存.cfg", "")), \
             mock.patch("ui.sftp_panel.QMessageBox.question") as question:
            panel._on_upload()
        question.assert_not_called()
        panel.sftp_manager.upload_file.assert_called_once()

    def test_works_without_a_config_manager(self):
        """config_manager 未指定でも既定値で動くこと。"""
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        self.assertTrue(panel._get_sftp_setting("confirm_delete", True))
        self.assertFalse(panel._get_sftp_setting("show_hidden_files", False))


if __name__ == "__main__":
    unittest.main()
