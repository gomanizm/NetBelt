"""st_mode を返さない項目があっても、SFTP の一覧が丸ごと消えないことを検証する。

SFTP v3 では permissions は省略可能な属性で、paramiko は ATTR_PERMISSIONS
フラグの無い項目の st_mode を None のままにする。SFTPManager はそれを
stat.S_ISDIR() に渡していたので、1 項目でも欠けていると TypeError で
listdir 全体が失敗し、正常な項目まで含めてパネルに何も出なかった
（実測: file_list_ready=[]、error_occurred=['... an integer is required']）。

欠けている項目はファイルとして扱い、パーミッション欄は「不明」と出す。
0 に丸めて「---------」と出すと、本当に権限の無いファイルと区別できない。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpManagerMissingModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_is_directory_treats_a_missing_mode_as_a_file(self):
        from core.sftp_manager import SFTPManager
        self.assertIs(SFTPManager._is_directory(None), False)

    def test_format_permissions_returns_none_for_a_missing_mode(self):
        """表示側が「不明」と出せるよう、偽の権限文字列を作らない。"""
        from core.sftp_manager import SFTPManager
        self.assertIsNone(SFTPManager._format_permissions(None))

    def test_a_listing_with_one_modeless_entry_still_delivers_the_rest(self):
        """1 項目の st_mode が None でも、一覧全体が届くこと。"""
        import paramiko
        from core.sftp_manager import SFTPManager

        bare = paramiko.SFTPAttributes()          # flags=0 で復号された項目相当
        bare.filename = "weird"
        self.assertIsNone(bare.st_mode, "前提: 素の SFTPAttributes の st_mode は None")

        regular = paramiko.SFTPAttributes()
        regular.filename = "running-config"
        regular.st_mode = 0o100644
        regular.st_size = 10
        folder = paramiko.SFTPAttributes()
        folder.filename = "flash"
        folder.st_mode = 0o040755

        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.sftp_client.listdir_attr.return_value = [bare, regular, folder]
        listings, errors = [], []
        m.file_list_ready.connect(listings.append)
        m.error_occurred.connect(errors.append)

        m.list_directory("/")
        self.assertTrue(self._wait(lambda: listings or errors),
                        "一覧もエラーも届かない")
        self.assertEqual(errors, [], "st_mode 無しの項目で一覧全体が失敗している")
        names = [f["name"] for f in listings[0]]
        self.assertEqual(sorted(names), ["flash", "running-config", "weird"],
                         "st_mode 無しの項目のせいで他の項目が消えている")
        weird = next(f for f in listings[0] if f["name"] == "weird")
        self.assertIs(weird["is_dir"], False)
        self.assertIsNone(weird["mode"])


class SftpPanelMissingModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from ui.sftp_panel import SFTPPanel
        return SFTPPanel()

    @staticmethod
    def _modeless_entry():
        return {
            'name': 'weird',
            'size': 10,
            'mtime': 1700000000,
            'mode': None,
            'is_dir': False,
            'permissions': None,
        }

    def test_a_modeless_entry_is_drawn_with_unknown_permissions(self):
        from ui.sftp_panel import SFTPPanel
        panel = self._panel()
        panel._update_file_list([self._modeless_entry()])
        self.assertEqual(panel.model.rowCount(), 1,
                         "st_mode 無しの項目で行が作られていない")
        self.assertEqual(panel.model.item(0, 2).text(), SFTPPanel.UNKNOWN_TEXT,
                         "不明なパーミッションが「不明」と出ていない")

    def test_chmod_dialog_opens_for_a_modeless_entry(self):
        """mode が None の項目でパーミッション変更を選んでも落ちないこと。"""
        from ui import sftp_panel as mod
        panel = self._panel()
        panel.sftp_manager = mock.Mock()
        panel.sftp_manager.get_current_path.return_value = "/"
        with mock.patch.object(mod.QInputDialog, "getText",
                               return_value=("", False)) as get_text:
            panel._on_chmod_selected(self._modeless_entry())
        get_text.assert_called_once()
        panel.sftp_manager.change_permissions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
