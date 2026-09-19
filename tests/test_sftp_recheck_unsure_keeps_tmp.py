"""転送後の再確認が権限エラーで有無を確かめられなかったとき、転送した一時名を後始末が消さないことを検証する。

overwrite=False の送信は、put のあと最終名へ改名する直前にもう一度 stat で
確かめる。これが権限エラー（_REMOTE_UNSURE）なら置き換えず、転送した内容は
一時名に残して知らせる（keep_tmp[0] = True）。残さないと、except の後始末が
一時名を remove し、送った内容を失ったうえで「一時名 … に残っています」と
事実と違う案内をする。

1 周目（sftpc-08）で、置き換えに失敗した分岐の一時名を実際の名前で確かめる
テストを足したが、この再確認の分岐は守られていなかった。
test_sftp_upload_recheck_timeout.py の
test_a_recheck_refused_by_permissions_is_not_reported_as_a_new_file は文面に
一時名が入っていることしか見ておらず、文面は keep_tmp と関係なく組み立て
られる。実測: この分岐の keep_tmp[0] = True を消す変異を入れると、後始末が
put に渡した一時名を remove した（removed が
['/flash/.running.cfg.netbelt-part.10280-5ec8d177']）のに、ほかの sftp の
テスト（281 件）はすべて通った。

直し方（テストのみ）: put に渡された実際の一時名が remove されていないこと、
remove が 1 回も呼ばれていないこと（最終名にも触れない）、文面にその名前が
入っていることを確かめる。同じ変異で落ちることを確かめてある。
"""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

FINAL = "/flash/running.cfg"


class SftpRecheckUnsureKeepsTmpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-recheck-unsure-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_the_temporary_name_is_kept_when_the_recheck_is_refused(self):
        """再確認が権限エラーなら、put に渡した一時名を消さずに知らせること。"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        client = m.sftp_client = mock.Mock()
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        errors = []
        m.error_occurred.connect(errors.append)
        # 送る前は「無い」。転送後の再確認は権限エラーで有無が分からない
        client.stat.side_effect = [IOError("No such file"),
                                   PermissionError("Permission denied")]

        m.upload_file(self.local, FINAL, overwrite=False)

        self.assertTrue(self._wait(lambda: errors), "失敗が通知されない")
        tmp = client.put.call_args[0][1]
        self.assertIn(".running.cfg.netbelt-part.", tmp)
        removed = [c[0][0] for c in client.remove.call_args_list]
        self.assertNotIn(tmp, removed, "転送した内容（一時名）を後始末が消している")
        self.assertEqual(removed, [], "remove を呼んでいる: %s" % removed)
        client.posix_rename.assert_not_called()
        client.rename.assert_not_called()
        self.assertIn(tmp, errors[0], "機器に残った一時名を知らせていない: %s" % errors)


if __name__ == "__main__":
    unittest.main()
