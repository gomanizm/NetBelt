# -*- coding: utf-8 -*-
"""FTP のルートを作れないとき、起動の失敗として知らせることを検証する。

start() の os.makedirs(root_dir) が try の外にあり、作れない場所（ファイルの下、
存在しないドライブ、書けない場所）を指定すると OSError がスロットの外へ出て、
「予期しないエラーが発生しました」の一般エラーのダイアログになっていた。
TFTP・SFTP と同じく error_occurred で理由を出し、False を返す。
"""
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, "src")


class FtpRootCreateFailureTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_uncreatable_root_is_reported_as_a_start_failure(self):
        patch = unittest.mock.patch(
            "core.firewall.ensure_inbound_allow", return_value=(True, "test stub"))
        patch.start()
        self.addCleanup(patch.stop)
        from core.ftp_server import FTPServerManager
        d = tempfile.mkdtemp(prefix="netbelt-ftp-rootfail-")
        blocker = os.path.join(d, "a-file")
        with open(blocker, "wb") as fh:
            fh.write(b"x")
        m = FTPServerManager()
        self.addCleanup(m.stop)
        errors = []
        m.error_occurred.connect(errors.append)
        ok = m.start(port=0, root_dir=os.path.join(blocker, "root"), anonymous=True)
        self.assertFalse(ok)
        self.assertFalse(m.is_running)
        self.assertTrue(any("ルートディレクトリの作成に失敗" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
