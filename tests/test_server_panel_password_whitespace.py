"""FTP/SFTP パネルが、パスワードの前後空白を黙って削らないことを検証する。

両パネルは password_edit.text().strip() の結果を、そのままサーバへ渡し
（FTP は設定ファイルへも保存し）ていた。パスワードの前後に空白を含む
利用者は、画面に見えているとおりの文字列ではログインできず、FTP では
削られた値が保存されるので次回以降も食い違ったままになる。

空欄かどうかの判定と、実際に使う値の正規化は別物である。未入力チェック
だけ strip() で行い、サーバへ渡す値・設定へ保存する値は入力そのままを
使う。全角空白ではなく半角空白だけのパスワードは、これまでどおり
「未入力」として起動を止める。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

PADDED_PASSWORD = "  pass word  "


class ServerPanelPasswordWhitespaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        workdir = tempfile.mkdtemp(prefix="netbelt-pwspace-")
        return ConfigManager(os.path.join(workdir, "config.json"))

    def _ftp_panel(self):
        from ui.ftp_server_panel import FTPServerPanel
        panel = FTPServerPanel(config_manager=self._config())
        panel.username_edit.setText("operator")
        return panel

    def _sftp_panel(self):
        from ui.sftp_server_panel import SFTPServerPanel
        panel = SFTPServerPanel()
        panel.username_edit.setText("operator")
        return panel

    def test_ftp_panel_passes_the_password_unchanged(self):
        """FTP: 前後空白つきのパスワードが、削られずにサーバへ渡ること。"""
        panel = self._ftp_panel()
        panel.password_edit.setText(PADDED_PASSWORD)
        with mock.patch.object(panel.ftp_server, "start",
                               return_value=True) as start:
            panel._on_start_server()
        start.assert_called_once()
        self.assertEqual(start.call_args.args[3], PADDED_PASSWORD,
                         "パスワードの前後空白が削られている")

    def test_ftp_panel_saves_the_password_unchanged(self):
        """FTP: 設定にも、削られていないパスワードが保存されること。"""
        panel = self._ftp_panel()
        panel.password_edit.setText(PADDED_PASSWORD)
        with mock.patch.object(panel.ftp_server, "start", return_value=True):
            panel._on_start_server()
        saved = panel.config_manager.get_server_settings("ftp_server")
        self.assertEqual(saved.get("password"), PADDED_PASSWORD,
                         "保存されたパスワードの前後空白が削られている")

    def test_ftp_panel_still_refuses_a_whitespace_only_password(self):
        """FTP: 空白だけのパスワードは、これまでどおり起動を止めること（対照）。"""
        panel = self._ftp_panel()
        panel.password_edit.setText("   ")
        with mock.patch.object(panel.ftp_server, "start",
                               return_value=True) as start, \
                mock.patch("ui.ftp_server_panel.QMessageBox.warning") as warn:
            panel._on_start_server()
        start.assert_not_called()
        warn.assert_called_once()

    def test_sftp_panel_passes_the_password_unchanged(self):
        """SFTP: 前後空白つきのパスワードが、削られずにサーバへ渡ること。"""
        panel = self._sftp_panel()
        panel.password_edit.setText(PADDED_PASSWORD)
        with mock.patch.object(panel.sftp_server, "start",
                               return_value=True) as start:
            panel._on_start_server()
        start.assert_called_once()
        self.assertEqual(start.call_args.args[3], PADDED_PASSWORD,
                         "パスワードの前後空白が削られている")

    def test_sftp_panel_still_refuses_a_whitespace_only_password(self):
        """SFTP: 空白だけのパスワードは、これまでどおり起動を止めること（対照）。"""
        panel = self._sftp_panel()
        panel.password_edit.setText("   ")
        with mock.patch.object(panel.sftp_server, "start",
                               return_value=True) as start, \
                mock.patch("ui.sftp_server_panel.QMessageBox.warning") as warn:
            panel._on_start_server()
        start.assert_not_called()
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
