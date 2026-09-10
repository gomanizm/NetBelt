"""FTP サーバで、ユーザー名 "anonymous" を通常ユーザーとして登録しないことを検証する。

pyftpdlib の DummyAuthorizer は、ユーザー名が "anonymous" のときパスワードの
照合を省略する（validate_authentication が anonymous を特別扱いする）。
NetBelt はパネルで入力されたユーザー名をそのまま add_user() に渡していたので、
「匿名を許可しない」設定でもユーザー名に anonymous と入れると、任意の
パスワードで elradfmwMT（削除・改名・書き込み）の全権限が通ってしまう。

匿名を使いたいなら「匿名を許可」の経路（権限を絞った add_anonymous）が
あるので、通常ユーザーとしての anonymous は起動時に拒否する。
"""
import ftplib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class FtpAnonymousUsernameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patcher = mock.patch("core.firewall.ensure_inbound_allow",
                             return_value=(True, "test stub"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-anon-")

    def _manager(self):
        from core.ftp_server import FTPServerManager
        m = FTPServerManager()
        self.addCleanup(m.stop)
        errors = []
        m.error_occurred.connect(errors.append)
        return m, errors

    def test_the_username_anonymous_is_refused_as_a_normal_user(self):
        """匿名を許可していないのに anonymous を登録すると、起動を拒否すること。"""
        m, errors = self._manager()

        started = m.start(port=0, root_dir=self.root,
                          username="anonymous", password="secret", anonymous=False)

        self.assertFalse(started, "anonymous を通常ユーザーとして起動している")
        self.assertTrue(any("anonymous" in e for e in errors),
                        "理由を言っていない: %s" % errors)

    def test_the_check_is_case_insensitive(self):
        """pyftpdlib は大文字小文字を区別しないので、こちらも区別しないこと。"""
        m, errors = self._manager()
        self.assertFalse(m.start(port=0, root_dir=self.root,
                                 username="Anonymous", password="secret",
                                 anonymous=False))

    def test_a_wrong_password_for_a_normal_user_is_still_refused(self):
        """通常ユーザーの認証はこれまでどおり効くこと（対照）。"""
        m, errors = self._manager()
        self.assertTrue(m.start(port=0, root_dir=self.root,
                                username="operator", password="secret",
                                anonymous=False))
        f = ftplib.FTP()
        self.addCleanup(f.close)
        f.connect("127.0.0.1", m.port, timeout=5)
        with self.assertRaises(ftplib.error_perm):
            f.login("operator", "wrong")

    def test_anonymous_login_still_works_when_explicitly_allowed(self):
        """「匿名を許可」の経路は変えないこと（対照）。"""
        m, errors = self._manager()
        self.assertTrue(m.start(port=0, root_dir=self.root,
                                username="", password="", anonymous=True))
        f = ftplib.FTP()
        self.addCleanup(f.close)
        f.connect("127.0.0.1", m.port, timeout=5)
        f.login("anonymous", "guest@example.com")   # 例外が出なければログイン成功
        self.assertEqual(f.pwd(), "/")


if __name__ == "__main__":
    unittest.main()
