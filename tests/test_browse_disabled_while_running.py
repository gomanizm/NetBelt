"""起動中はルートディレクトリの「参照」ボタンも押せないことを確認する。

ルート欄（QLineEdit）は起動中に無効化されるが、隣の「参照」ボタンは
無効化の対象から漏れていた。QLineEdit.setText() は無効化済みの欄にも
効くので、起動中に参照を押すと画面のルートだけが別のフォルダへ変わり、
実際に公開しているルートと食い違う（実測: 画面は rootB、アップロードは
rootA へ落ちる）。TFTP / FTP / SFTP の 3 パネルすべてで起きる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class BrowseDisabledWhileRunningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        workdir = tempfile.mkdtemp(prefix="netbelt-browse-")
        return ConfigManager(os.path.join(workdir, "config.json"))

    def _check(self, panel, server_attr):
        """起動中は参照が無効、停止で戻ること。サーバ本体は起こさない。"""
        server = getattr(panel, server_attr)
        with mock.patch.object(server, "start", return_value=True), \
                mock.patch.object(server, "stop"):
            self.assertTrue(hasattr(panel, "browse_btn"),
                            "参照ボタンが属性として持たれていない")
            self.assertTrue(panel.browse_btn.isEnabled())
            panel._on_start_server()
            self.assertFalse(panel.root_dir_edit.isEnabled(),
                             "前提: ルート欄は起動中に無効化される")
            self.assertFalse(panel.browse_btn.isEnabled(),
                             "起動中でも参照ボタンが押せる")
            panel._on_stop_server()
            self.assertTrue(panel.browse_btn.isEnabled(),
                            "停止しても参照ボタンが戻らない")

    def test_tftp_browse_is_disabled_while_running(self):
        from ui.tftp_server_panel import TFTPServerPanel
        panel = TFTPServerPanel(config_manager=self._config())
        self._check(panel, "tftp_server")

    def test_ftp_browse_is_disabled_while_running(self):
        from ui.ftp_server_panel import FTPServerPanel
        panel = FTPServerPanel(config_manager=self._config())
        panel.username_edit.setText("netbelt")
        panel.password_edit.setText("pw")
        self._check(panel, "ftp_server")

    def test_sftp_browse_is_disabled_while_running(self):
        from ui.sftp_server_panel import SFTPServerPanel
        panel = SFTPServerPanel()
        panel.username_edit.setText("netbelt")
        panel.password_edit.setText("pw")
        self._check(panel, "sftp_server")


if __name__ == "__main__":
    unittest.main()
