"""setuid / setgid / sticky が、一覧表示と chmod ダイアログから消えないことを検証する。

パーミッション欄は 9 文字（rwxrwxrwx）しか組み立てず、chmod ダイアログの
既定値も mode & 0o777 だった。そのため /tmp のような sticky 付き
ディレクトリや setuid 付きのバイナリは、画面上は普通の 777 / 755 に見え、
利用者が何も書き換えずに OK を押しただけで特殊ビットを落とした mode が
chmod で送られる（paramiko の SFTPClient.chmod は attr.st_mode = mode と
渡された値をそのまま設定する）。

実測:
  mode=0o41777  -> 表示 'drwxrwxrwx'、既定値 '777' -> change_permissions(..., 0o777)
  mode=0o104755 -> 表示 '-rwxr-xr-x'、既定値 '755' -> change_permissions(..., 0o755)

ls と同じ表記（s/S/t/T）で見せ、既定値を 4 桁にして、無変更の OK では
ビットが変わらないようにする。
"""
import os
import stat
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class FormatPermissionsSpecialBitsTest(unittest.TestCase):
    def test_sticky_directory_is_shown_with_t(self):
        from core.sftp_manager import SFTPManager
        self.assertEqual(
            SFTPManager._format_permissions(stat.S_IFDIR | 0o1777),
            "drwxrwxrwt")

    def test_sticky_without_execute_is_shown_with_capital_t(self):
        from core.sftp_manager import SFTPManager
        self.assertEqual(
            SFTPManager._format_permissions(stat.S_IFDIR | 0o1666),
            "drw-rw-rwT")

    def test_setuid_is_shown_with_s(self):
        from core.sftp_manager import SFTPManager
        self.assertEqual(
            SFTPManager._format_permissions(stat.S_IFREG | 0o4755),
            "-rwsr-xr-x")

    def test_setuid_without_execute_is_shown_with_capital_s(self):
        from core.sftp_manager import SFTPManager
        self.assertEqual(
            SFTPManager._format_permissions(stat.S_IFREG | 0o4644),
            "-rwSr--r--")

    def test_setgid_is_shown_with_s(self):
        from core.sftp_manager import SFTPManager
        self.assertEqual(
            SFTPManager._format_permissions(stat.S_IFREG | 0o2755),
            "-rwxr-sr-x")

    def test_setgid_without_execute_is_shown_with_capital_s(self):
        from core.sftp_manager import SFTPManager
        self.assertEqual(
            SFTPManager._format_permissions(stat.S_IFREG | 0o2644),
            "-rw-r-Sr--")

    def test_plain_modes_keep_their_current_look(self):
        """特殊ビットの無い項目の表示は今までどおりであること。"""
        from core.sftp_manager import SFTPManager
        self.assertEqual(
            SFTPManager._format_permissions(stat.S_IFREG | 0o644),
            "-rw-r--r--")
        self.assertEqual(
            SFTPManager._format_permissions(stat.S_IFDIR | 0o755),
            "drwxr-xr-x")


class ChmodDialogSpecialBitsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls._panels = []

    def _panel_with_manager(self):
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        type(self)._panels.append(panel)      # 親より先に捨てない
        manager = mock.Mock()
        manager.current_path = "/"
        manager.get_current_path.return_value = "/"
        panel.set_sftp_manager(manager, "rtrA")
        manager.reset_mock()
        manager.get_current_path.return_value = "/"
        return panel, manager

    @staticmethod
    def _entry(name, mode, is_dir=False):
        from core.sftp_manager import SFTPManager
        return {
            'name': name,
            'size': 0,
            'mtime': 1700000000,
            'mode': mode,
            'is_dir': is_dir,
            'permissions': SFTPManager._format_permissions(mode),
        }

    def _chmod_with_unchanged_default(self, entry):
        """ダイアログの既定値をそのまま OK したときの (既定値, 送った mode)。"""
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()
        seen = {}

        def accept_the_default(parent, title, label, text="", *args, **kwargs):
            seen['default'] = text
            return (text, True)

        with mock.patch.object(mod.QInputDialog, "getText",
                               side_effect=accept_the_default):
            panel._on_chmod_selected(entry)
        manager.change_permissions.assert_called_once()
        return seen['default'], manager.change_permissions.call_args[0][1]

    def test_sticky_directory_survives_an_unchanged_ok(self):
        default, sent = self._chmod_with_unchanged_default(
            self._entry("tmp", 0o41777, is_dir=True))
        self.assertEqual(default, "1777", "既定値に sticky が出ていない")
        self.assertEqual(oct(sent), oct(0o1777), "無変更の OK で sticky が落ちた")

    def test_setuid_file_survives_an_unchanged_ok(self):
        default, sent = self._chmod_with_unchanged_default(
            self._entry("ping", 0o104755))
        self.assertEqual(default, "4755", "既定値に setuid が出ていない")
        self.assertEqual(oct(sent), oct(0o4755), "無変更の OK で setuid が落ちた")

    def test_setgid_file_survives_an_unchanged_ok(self):
        default, sent = self._chmod_with_unchanged_default(
            self._entry("wall", 0o102755))
        self.assertEqual(default, "2755", "既定値に setgid が出ていない")
        self.assertEqual(oct(sent), oct(0o2755), "無変更の OK で setgid が落ちた")

    def test_a_plain_file_default_is_padded_to_four_digits(self):
        default, sent = self._chmod_with_unchanged_default(
            self._entry("running-config", 0o100644))
        self.assertEqual(default, "0644", "既定値が 4 桁で出ていない")
        self.assertEqual(oct(sent), oct(0o644))

    def test_three_digit_input_still_means_what_it_says(self):
        """3 桁を打てば、これまでどおり特殊ビットの無い mode を送ること。"""
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()
        with mock.patch.object(mod.QInputDialog, "getText",
                               return_value=("755", True)):
            panel._on_chmod_selected(self._entry("tmp", 0o41777, is_dir=True))
        manager.change_permissions.assert_called_once()
        self.assertEqual(oct(manager.change_permissions.call_args[0][1]),
                         oct(0o755))


if __name__ == "__main__":
    unittest.main()
