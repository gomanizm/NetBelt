"""config.json のサーバー設定の文字の欄が手書きで数や配列になっていても、FTP / TFTP のパネルが作れること。

何が起きていたか（実測、441ea02 から f525532 まで。ネットワークは使わない）:
settings.ftp_server の username / password / root_directory と settings.tftp_server の
root_directory に、引用符を付け忘れた数（"password": 1234）や 1e309、配列を手で書くと、
パネルの _restore_settings はその値をそのまま QLineEdit.setText へ渡しており、
'TypeError: setText(self, a0: Optional[str]): argument 1 has unexpected type 'int''
でパネルの構築が失敗した。パネルは MainWindow.__init__ から作られるので、値が
残っている限りアプリが起動しない（ポートの欄を直した
tests/test_server_panel_port_restore.py と同じ種類で、別の欄）。

どう直したか: 文字の欄は文字列のときだけ戻す。それ以外は未設定として扱い、
ユーザー名・パスワードは空、ルートは専用フォルダの既定値（./ftp_root / ./tftp_root）の
まま起動する。数を文字へ直して資格情報やルートを推測することはしない。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

NOT_TEXT = ("1234", "1e309", "-1e309", "12.5", "true", "[1]", '["a"]', "{}")


def _config_text(section, fields):
    """settings.<section> に fields（欄の名前 → JSON の字面そのまま）を書いた config.json の中身"""
    body = ", ".join('"%s": %s' % (name, literal)
                     for name, literal in fields.items())
    return ('{"config_version": "1.0", "groups": [{"name": "Default", '
            '"auto_commands": [], "devices": []}], "global_macros": [], '
            '"settings": {"%s": {%s}}, '
            '"update_settings": {"check_on_startup": false}}'
            % (section, body))


class ServerPanelTextRestoreTest(unittest.TestCase):
    _keep = []   # 配送待ちシグナルの宛先を先に解放しない（他テストと同じ理由）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()
        cls._keep.clear()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-panel-text-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        home = os.path.join(self.dir, "home")
        os.mkdir(home)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=Path(home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _panel(self, section, fields):
        from core.config_manager import ConfigManager
        from ui.ftp_server_panel import FTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_config_text(section, fields))
        cm = ConfigManager(config_path=path)
        # 前提: 読み込みの経路（_load_config / 復号）を通っても値が残っている
        self.assertIsNone(cm.load_error)
        section_dict = cm.get_server_settings(section)
        for name, literal in fields.items():
            self.assertIn(name, section_dict)
            if literal in NOT_TEXT:
                self.assertNotIsInstance(section_dict[name], str)
        cls = FTPServerPanel if section == "ftp_server" else TFTPServerPanel
        panel = cls(config_manager=cm)
        self._keep.append(panel)
        return panel

    def test_ftp_credentials_that_are_not_text_are_left_empty(self):
        for field in ("username", "password"):
            for literal in NOT_TEXT:
                with self.subTest(field=field, literal=literal):
                    panel = self._panel("ftp_server", {field: literal})
                    edit =(panel.username_edit if field == "username"
                            else panel.password_edit)
                    self.assertEqual(edit.text(), "")

    def test_root_directory_that_is_not_text_falls_back_to_the_default(self):
        for section, default in (("ftp_server", "./ftp_root"),
                                 ("tftp_server", "./tftp_root")):
            for literal in NOT_TEXT:
                with self.subTest(section=section, literal=literal):
                    panel = self._panel(section, {"root_directory": literal})
                    self.assertEqual(panel.root_dir_edit.text(), default)

    def test_text_values_are_still_restored(self):
        panel = self._panel("ftp_server", {
            "root_directory": '"./example-root"', "username": '"example-user"',
            "password": '" example pass "'})
        self.assertEqual(panel.root_dir_edit.text(), "./example-root")
        self.assertEqual(panel.username_edit.text(), "example-user")
        # パスワードは前後の空白も資格情報の一部なので、そのまま戻す
        self.assertEqual(panel.password_edit.text(), " example pass ")
        panel = self._panel("tftp_server", {"root_directory": '"./example-root"'})
        self.assertEqual(panel.root_dir_edit.text(), "./example-root")

    def test_other_settings_are_still_restored(self):
        """文字の欄が使えなくても、同じセクションのほかの設定は読み込まれる"""
        panel = self._panel("ftp_server", {
            "root_directory": "[1]", "username": "1234", "password": "1234",
            "port": "2121", "anonymous": "true"})
        self.assertEqual(panel.port_spin.value(), 2121)
        self.assertTrue(panel.anonymous_check.isChecked())
        panel = self._panel("tftp_server", {"root_directory": "1e309",
                                            "port": "2121"})
        self.assertEqual(panel.port_spin.value(), 2121)


if __name__ == "__main__":
    unittest.main()
