# -*- coding: utf-8 -*-
"""一覧の既存名と綴りの大小だけ違う名前を送るとき、上書きの確認に到達できることを検証する。

パネルの上書き判定は一覧の名前との完全一致だったので、一覧に config.txt が
あるとき Config.txt を送ると「既存なし」として overwrite=False で送っていた。
大文字小文字を区別しない機器（NetBelt 内蔵の SFTP サーバー、OpenSSH for
Windows など）では、送る直前の stat が既存を見つけて「一覧を更新してから
やり直してください」と断る。更新しても一覧の綴りは変わらないので、何度
やっても同じ断りになり、上書きの確認には一度も到達できなかった。

綴りの大小だけ違う既存のファイルがあれば、それを名指しして上書きの確認を出し、
承認されたら上書きを許して送ること。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpUploadCaseOnlyNameTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self, entries):
        from core.config_manager import ConfigManager
        from ui.sftp_panel import SFTPPanel
        d = tempfile.mkdtemp(prefix="netbelt-sftp-case-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        panel = SFTPPanel(config_manager=cm)
        panel.sftp_manager = mock.Mock()
        panel.sftp_manager.get_current_path.return_value = "/home/admin"
        panel._update_file_list([
            {"name": name, "is_dir": is_dir, "size": 10,
             "permissions": "-rw-r--r--", "mtime": 0}
            for name, is_dir in entries])
        type(self)._keep += [panel, cm]
        return panel

    def test_a_case_only_match_asks_before_sending(self):
        from PyQt6.QtWidgets import QMessageBox
        panel = self._panel([("config.txt", False)])
        with mock.patch("ui.sftp_panel.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes) as question:
            panel._upload_with_confirmation("C:/tmp/Config.txt")
        question.assert_called_once()
        text = question.call_args[0][2]
        self.assertIn("'config.txt'", text, "既存の綴りを名指ししていない")
        self.assertIn("'Config.txt'", text)
        panel.sftp_manager.upload_file.assert_called_once_with(
            "C:/tmp/Config.txt", "/home/admin/Config.txt", overwrite=True)

    def test_declining_the_case_only_overwrite_sends_nothing(self):
        from PyQt6.QtWidgets import QMessageBox
        panel = self._panel([("config.txt", False)])
        with mock.patch("ui.sftp_panel.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.No):
            panel._upload_with_confirmation("C:/tmp/Config.txt")
        panel.sftp_manager.upload_file.assert_not_called()

    def test_an_exact_match_keeps_the_usual_question(self):
        from PyQt6.QtWidgets import QMessageBox
        panel = self._panel([("config.txt", False), ("Config.txt", False)])
        with mock.patch("ui.sftp_panel.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.No) as question:
            panel._upload_with_confirmation("C:/tmp/Config.txt")
        question.assert_called_once()
        self.assertEqual(question.call_args[0][2],
                         "リモートに 'Config.txt' が既にあります。上書きしますか？")

    def test_a_case_only_match_with_a_directory_does_not_ask(self):
        panel = self._panel([("configs", True)])
        with mock.patch("ui.sftp_panel.QMessageBox.question") as question:
            panel._upload_with_confirmation("C:/tmp/Configs")
        question.assert_not_called()
        panel.sftp_manager.upload_file.assert_called_once_with(
            "C:/tmp/Configs", "/home/admin/Configs", overwrite=False)


if __name__ == "__main__":
    unittest.main()
