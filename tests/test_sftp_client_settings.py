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

    def test_hidden_files_are_still_protected_from_overwrite(self):
        """表示していないだけで上書き保護が外れてはいけない。"""
        from PyQt6.QtWidgets import QMessageBox
        panel, _ = self._panel()   # 隠しファイルは非表示（既定）
        panel._update_file_list([self._entry("visible.txt"), self._entry(".hidden.cfg")])
        self.assertEqual(panel.model.rowCount(), 1, "隠しファイルは表示されない")

        with mock.patch("ui.sftp_panel.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.No) as question:
            panel._upload_with_confirmation("C:/tmp/.hidden.cfg")
        question.assert_called_once()
        panel.sftp_manager.upload_file.assert_not_called()

    def test_same_name_directory_is_not_called_an_overwrite(self):
        """同名がディレクトリなら上書きにはならない（upload は失敗する）。"""
        panel, _ = self._panel()
        panel._update_file_list([self._entry("conf", is_dir=True)])
        with mock.patch("ui.sftp_panel.QMessageBox.question") as question:
            panel._upload_with_confirmation("C:/tmp/conf")
        question.assert_not_called()

    def test_second_upload_of_the_same_name_in_one_drop_is_confirmed(self):
        """一覧の取り直しが間に合わない間も確認すること。"""
        from PyQt6.QtWidgets import QMessageBox
        panel, _ = self._panel()
        panel._update_file_list([])
        with mock.patch("ui.sftp_panel.QMessageBox.question") as question:
            panel._upload_with_confirmation("C:/tmp/a/same.cfg")
        question.assert_not_called()

        with mock.patch("ui.sftp_panel.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.No) as question:
            panel._upload_with_confirmation("C:/tmp/b/same.cfg")
        question.assert_called_once()

    def test_clear_drops_the_listing_cache(self):
        """再接続直後に古い名前で判定しないこと。"""
        panel, _ = self._panel()
        panel._update_file_list([self._entry("残骸.cfg")])
        panel.clear()
        self.assertEqual(panel._current_entries, {})
        self.assertEqual(panel._pending_upload_names, set())

    def test_upload_does_not_change_the_observed_entry_kind(self):
        """送信しても「最後に観測した一覧」を書き換えないこと。

        同名ディレクトリへ送って失敗したあと、その名前が「ファイル」として
        残ると、再試行で誤って上書き確認が出る。
        """
        panel, _ = self._panel()
        panel._update_file_list([self._entry("conf", is_dir=True)])
        with mock.patch("ui.sftp_panel.QMessageBox.question") as question:
            panel._upload_with_confirmation("C:/tmp/conf")
        question.assert_not_called()
        self.assertIs(panel._current_entries["conf"], True,
                      "観測した種別が送信で書き換わっている")

        # 一覧が更新される前に再試行しても、まだディレクトリ扱いのまま
        with mock.patch("ui.sftp_panel.QMessageBox.question") as question:
            panel._upload_with_confirmation("C:/tmp/conf")
        question.assert_not_called()

    def test_a_fresh_listing_forgets_pending_uploads(self):
        """一覧が来たらそれが真実。送信中として覚えていた名前は捨てる。"""
        panel, _ = self._panel()
        panel._update_file_list([])
        panel._upload_with_confirmation("C:/tmp/new.cfg")
        self.assertIn("new.cfg", panel._pending_upload_names)

        panel._update_file_list([])   # 一覧を取り直した（まだ現れていない）
        self.assertEqual(panel._pending_upload_names, set())

    def test_drop_goes_through_the_overwrite_confirmation(self):
        """ドラッグ&ドロップ経路もボタンと同じ確認を通ること。"""
        panel, _ = self._panel()
        panel._update_file_list([self._entry("既存.cfg")])

        event = mock.Mock()
        url = mock.Mock()
        url.toLocalFile.return_value = "C:/tmp/既存.cfg"
        event.mimeData.return_value.urls.return_value = [url]

        with mock.patch("ui.sftp_panel.os.path.isfile", return_value=True), \
             mock.patch.object(panel, "_upload_with_confirmation") as upload:
            panel.dropEvent(event)
        # 引数の個数はここで見ない。操作を始めた時点のマネージャを渡すように
        # なったため（test_sftp_panel_stale_manager.py）。ここで見たいのは
        # 「ドロップしたファイルが確認経路へ入ること」だけ。
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0], "C:/tmp/既存.cfg")

    def test_broken_boolean_settings_fall_back_to_defaults(self):
        """config.json は手で編集できるので型違いが来る。"""
        for broken in ("no", "yes", 0, 1, None):
            with self.subTest(show_hidden_files=broken):
                panel, _ = self._panel({"show_hidden_files": broken})
                panel._update_file_list([self._entry(".hidden"), self._entry("見える.txt")])
                self.assertEqual(panel.model.rowCount(), 1,
                                 "壊れた値が真として扱われている")

    def test_broken_download_path_falls_back_to_default(self):
        from ui.sftp_panel import SFTPPanel
        for broken in (123, "", "   ", None):
            with self.subTest(default_download_path=broken):
                panel, _ = self._panel({"default_download_path": broken})
                self.assertEqual(
                    panel._get_sftp_setting("default_download_path", None),
                    SFTPPanel.SFTP_SETTING_DEFAULTS["default_download_path"])


if __name__ == "__main__":
    unittest.main()
