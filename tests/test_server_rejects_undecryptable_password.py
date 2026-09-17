"""復号できない暗号文を、そのままサーバの認証パスワードにしないことを検証する。

PasswordCrypto.decrypt は復号に失敗すると入力（"DPAPI:..." の暗号文）を
そのまま返し、ConfigManager は件数を数えて print するだけで値は保持する。
別 PC / 別 Windows アカウントで保存した config.json を持ち込むと、FTP/SFTP
の画面は伏字で埋まって見えるのに、サーバは暗号文そのものをパスワードとして
受け付ける（実測: 暗号文でログイン 230、本来の秘密で 530）。利用者には
「原因の分からない認証失敗」にしか見えない。

サーバ側で暗号文を拒否し、起動を断って理由を知らせる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

FOREIGN = "DPAPI:" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="


class ServerRejectsUndecryptablePasswordTest(unittest.TestCase):
    # 作ったマネージャはクラス終了まで保持する（test_server_start_failure と同じ理由）
    _managers = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        cls._managers.clear()

    def setUp(self):
        from pathlib import Path
        home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-srvroot-")

    def _errors(self, manager):
        seen = []
        manager.error_occurred.connect(seen.append)
        return seen

    def test_ftp_refuses_to_start_with_a_ciphertext_password(self):
        from core.ftp_server import FTPServerManager
        mgr = FTPServerManager()
        self._managers.append(mgr)
        errors = self._errors(mgr)

        ok = mgr.start(port=0, root_dir=self.root, username="ftpuser",
                       password=FOREIGN)
        try:
            self.assertFalse(ok, "暗号文をパスワードとして受け付けて起動した")
            self.assertFalse(mgr.is_running)
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("復号", errors[0])
            self.assertNotIn(FOREIGN, errors[0], "暗号文をそのまま表示している")
        finally:
            if mgr.is_running:
                mgr.stop()

    def test_anonymous_only_ftp_still_starts_with_a_stale_ciphertext(self):
        """パスワードが認証に使われない匿名専用の構成は、これまでどおり起動すること。

        FTP は username と password の両方が非空のときだけ add_user() を
        呼ぶ（匿名専用なら暗号文は誰の資格情報にもならない）。設定に古い
        暗号文が残っているだけで匿名 FTP が起動しなくなると、いままで
        動いていた構成の回帰になる。
        """
        from core.ftp_server import FTPServerManager
        mgr = FTPServerManager()
        self._managers.append(mgr)
        self.addCleanup(mgr.stop)
        errors = self._errors(mgr)

        ok = mgr.start(port=0, root_dir=self.root, username="",
                       password=FOREIGN, anonymous=True)

        self.assertTrue(ok, "匿名専用の構成まで断っている: %s" % errors)
        self.assertEqual(errors, [])


    def test_sftp_refuses_to_start_with_a_ciphertext_password(self):
        from core.sftp_server import SFTPServerManager
        mgr = SFTPServerManager()
        self._managers.append(mgr)
        errors = self._errors(mgr)

        ok = mgr.start(port=0, root_dir=self.root, username="sftpuser",
                       password=FOREIGN)
        try:
            self.assertFalse(ok, "暗号文をパスワードとして受け付けて起動した")
            self.assertFalse(mgr.is_running)
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("復号", errors[0])
            self.assertNotIn(FOREIGN, errors[0], "暗号文をそのまま表示している")
        finally:
            if mgr.is_running:
                mgr.stop()


if __name__ == "__main__":
    unittest.main()
