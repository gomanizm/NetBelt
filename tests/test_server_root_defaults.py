"""受信サーバの既定ルートが、ユーザーの個人フォルダを指していないことを検証する。

TFTP はプロトコル上そもそも認証が無く 0.0.0.0 で待ち受けるため、既定ルートが
デスクトップやホームだと、既定のまま起動しただけで個人フォルダ全体が
無認証でネットワークへ読み書き公開される。専用フォルダに閉じ込める。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


def _personal_dirs():
    home = os.path.abspath(os.path.expanduser("~"))
    return [home, os.path.join(home, "Desktop"), os.path.join(home, "Documents")]


class ServerRootDefaultTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self, module_name, class_name):
        """設定が空の状態でパネルを作り、ルートディレクトリ欄の値を返す。"""
        from core.config_manager import ConfigManager
        import importlib
        mod = importlib.import_module(module_name)
        cls = getattr(mod, class_name)

        workdir = tempfile.mkdtemp(prefix="netbelt-rootdef-")
        cm = ConfigManager(os.path.join(workdir, "config.json"))
        # SFTP パネルだけ config_manager を受け取らない
        import inspect
        if "config_manager" in inspect.signature(cls.__init__).parameters:
            panel = cls(config_manager=cm)
        else:
            panel = cls()
        return panel.root_dir_edit.text()

    def _assert_not_personal(self, value, label):
        resolved = os.path.abspath(value)
        for p in _personal_dirs():
            self.assertNotEqual(
                resolved, os.path.abspath(p),
                "%s の既定ルートが個人フォルダ (%s) を指している" % (label, p))

    def test_tftp_default_root_is_dedicated_folder(self):
        got = self._panel("ui.tftp_server_panel", "TFTPServerPanel")
        self._assert_not_personal(got, "TFTP")
        self.assertIn("tftp_root", got,
                      "TFTP の既定ルートは専用フォルダであるべき: %r" % got)

    def test_ftp_default_root_is_dedicated_folder(self):
        got = self._panel("ui.ftp_server_panel", "FTPServerPanel")
        self._assert_not_personal(got, "FTP")
        self.assertIn("ftp_root", got,
                      "FTP の既定ルートは専用フォルダであるべき: %r" % got)

    def test_sftp_default_root_is_dedicated_folder(self):
        got = self._panel("ui.sftp_server_panel", "SFTPServerPanel")
        self._assert_not_personal(got, "SFTP")
        self.assertIn("sftp_root", got,
                      "SFTP の既定ルートは専用フォルダであるべき: %r" % got)

    def test_no_helper_returns_personal_folder_as_server_root(self):
        """デスクトップを返すヘルパーが残っていないこと。"""
        import core.config_manager as cm
        self.assertFalse(
            hasattr(cm, "default_server_root"),
            "default_server_root() は個人フォルダを既定ルートにするため廃止済みのはず")


if __name__ == "__main__":
    unittest.main()
