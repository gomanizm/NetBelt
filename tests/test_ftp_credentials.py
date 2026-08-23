"""FTP サーバの資格情報と匿名アクセスの既定を確認する。

SFTP と同じく、admin/admin が入力済みで表示されていると、そのまま「開始」を
押した利用者がネットワークへ弱い資格情報を晒す。匿名アクセスに書き込み権限を
与えると、認証なしでファイルを作成・上書き・削除できてしまう。
"""
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, "src")


class FtpCredentialsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-ftpcred-")
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _manager(self):
        from core.ftp_server import FTPServerManager
        m = FTPServerManager()
        self.addCleanup(m.stop)
        return m

    def test_empty_credentials_are_rejected(self):
        """匿名を許可せず、資格情報も空なら起動しないこと。"""
        m = self._manager()
        errors = []
        m.error_occurred.connect(errors.append)
        self.assertFalse(m.start(port=0, root_dir=self.root,
                                 username="", password="", anonymous=False))
        self.assertTrue(errors, "エラーが通知されていない")

    def test_partial_credentials_are_rejected(self):
        for user, pw in (("u", ""), ("", "p")):
            with self.subTest(username=user, password=pw):
                m = self._manager()
                self.assertFalse(m.start(port=0, root_dir=self.root,
                                         username=user, password=pw,
                                         anonymous=False))

    def test_authenticated_user_keeps_full_permissions(self):
        """認証ユーザーの権限はこれまでどおりであること。"""
        from pyftpdlib.authorizers import DummyAuthorizer
        seen = {}
        original = DummyAuthorizer.add_user

        def spy(self, username, password, homedir, **kwargs):
            seen["perm"] = kwargs.get("perm", "")
            return original(self, username, password, homedir, **kwargs)

        with unittest.mock.patch.object(DummyAuthorizer, "add_user", spy):
            m = self._manager()
            self.assertTrue(m.start(port=0, root_dir=self.root,
                                    username="ops", password="s3cret"))
        for ch in "elradfmwMT":
            self.assertIn(ch, seen.get("perm", ""))

    def test_panel_has_no_default_credentials(self):
        """パネルに資格情報が入力済みで表示されないこと。"""
        from core.config_manager import ConfigManager
        from ui.ftp_server_panel import FTPServerPanel
        workdir = tempfile.mkdtemp(prefix="netbelt-ftpcred-cfg-")
        cm = ConfigManager(os.path.join(workdir, "config.json"))
        panel = FTPServerPanel(config_manager=cm)
        self.assertEqual(panel.username_edit.text(), "",
                         "ユーザー名が既定で入力されている")
        self.assertEqual(panel.password_edit.text(), "",
                         "パスワードが既定で入力されている")
        self.assertTrue(panel.username_edit.placeholderText(),
                        "入力を促すプレースホルダが無い")
        self.assertFalse(panel.anonymous_check.isChecked(),
                         "匿名アクセスが既定で有効になっている")


if __name__ == "__main__":
    unittest.main()
