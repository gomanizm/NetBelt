"""置き換えに失敗して残した一時名（唯一の完全な写し）を、後始末が消さないことを検証する。

test_sftp_transfer_atomicity.py の
test_a_failed_rename_after_removing_the_target_keeps_the_temporary_copy は、
remove されていないことを固定名 '/flash/.running.cfg.netbelt-part' で
確かめている。いまの一時名には PID と UUID が付く
（'.running.cfg.netbelt-part.<pid>-<uuid>'）ので、この比較は常に通る。
実測: 最終名を消したあとの rename が失敗した分岐から keep_tmp[0] = True を
消す変異を入れると、後始末が一時名まで消した（removed が
['/flash/running.cfg', '/flash/.running.cfg.netbelt-part.8500-045ff40c']）
のに、そのテストファイルは 11 件とも通った。

直し方（テストのみ）: 既存のテストは変えずに、put に渡された実際の一時名が
remove されていないことを確かめるテストを足す。同じ変異で落ちることを
確かめてある。
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


class SftpKeptTemporaryCopyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-kept-copy-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        # リモートに同名は無い（stat が失敗する）
        self.client.stat.side_effect = IOError("No such file")
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors = []
        m.error_occurred.connect(self.errors.append)
        return m

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _fail_the_rename_after_removing_the_target(self, overwrite):
        """posix_rename の無いサーバで、最終名を消したあとの rename が失敗する"""
        m = self._manager()
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        # 1 回目の rename は「既にある」で失敗、remove 後の 2 回目は切断で失敗
        self.client.rename.side_effect = [IOError("Failure"),
                                          IOError("connection lost")]

        m.upload_file(self.local, FINAL, overwrite=overwrite)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        tmp = self.client.put.call_args[0][1]
        self.assertIn(".running.cfg.netbelt-part.", tmp)
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        return tmp, removed

    def test_the_actual_temporary_name_is_not_removed(self):
        """put に渡した実際の一時名を後始末が消さず、その名前を知らせること。

        上書きの確認を経ていない送信は、rename が断られても最終名を消す
        復旧手順へ進まなくなった（確認なしの置き換えになるため。
        test_sftp_unconfirmed_replace_keeps_final.py）。この経路では
        remove そのものを呼ばない。
        """
        tmp, removed = self._fail_the_rename_after_removing_the_target(overwrite=False)

        self.assertNotIn(tmp, removed, "唯一の完全な写し（一時名）を消している")
        self.assertEqual(removed, [], "確認を経ていない送信が消しにいっている: %s" % removed)
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)

    def test_the_actual_temporary_name_is_not_removed_on_an_overwrite(self):
        """上書きを承認済みの送信でも同じであること。"""
        tmp, removed = self._fail_the_rename_after_removing_the_target(overwrite=True)

        self.assertNotIn(tmp, removed, "唯一の完全な写し（一時名）を消している")
        self.assertEqual(removed, [FINAL], "最終名の remove が変わった: %s" % removed)
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)


if __name__ == "__main__":
    unittest.main()
