"""転送後の再確認 stat が期限切れ・権限エラーになったときの扱いを検証する。

upload_file は overwrite=False のとき、put のあと最終名へ改名する直前に
もう一度 _remote_probe を投げる。その結果を `state != _REMOTE_MISSING` で
ひとまとめにすると、次の2つが「転送しているあいだにリモートへ '…' が
作られました」という事実と違う断定になる。

- 期限切れ（_REMOTE_TIMEOUT）: 有無は確かめられていない。しかも送出例外が
  IOError なので _fail の期限切れ判定に掛からず、使えなくなったチャンネルを
  掴んだまま接続中の扱いが続く。
- 権限エラー（_REMOTE_UNSURE）: これも有無は確かめられていない。

送る直前の probe については同じ穴が塞がれている。転送後の probe からも
同じように到達できないこと。
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


class SftpUploadRecheckTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-recheck-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.sftp_client.normalize.side_effect = lambda p: p
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        return m

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_a_recheck_that_times_out_is_not_reported_as_a_new_file(self):
        """再確認が期限切れなら「作られました」と断定しないこと。"""
        m = self._manager()
        # 期限切れなら接続を畳んで sftp_client を手放すので、先に控える
        client = m.sftp_client
        # 1回目（送る直前）は「無い」、2回目（転送後の再確認）が期限切れ
        client.stat.side_effect = [IOError("No such file"), TimeoutError()]

        m.upload_file(self.local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertNotIn("が作られました", self.errors[0],
                         "確かめられていないのに断定している: %s" % self.errors)
        self.assertIn(".running.cfg.netbelt-part", self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        client.posix_rename.assert_not_called()
        client.rename.assert_not_called()
        removed = [c[0][0] for c in client.remove.call_args_list]
        self.assertEqual(removed, [], "転送した唯一の写しを消している: %s" % removed)

    def test_a_recheck_that_times_out_closes_the_connection(self):
        """再確認が期限切れなら、使えなくなった接続を畳むこと。"""
        m = self._manager()
        m.sftp_client.stat.side_effect = [IOError("No such file"), TimeoutError()]

        m.upload_file(self.local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertTrue(self._wait(lambda: not m.is_connected),
                        "期限切れなのに接続中のまま")

    def test_a_recheck_refused_by_permissions_is_not_reported_as_a_new_file(self):
        """再確認が権限エラーなら「作られました」と断定せず、接続は畳まないこと。"""
        m = self._manager()
        m.sftp_client.stat.side_effect = [IOError("No such file"),
                                          PermissionError("Permission denied")]

        m.upload_file(self.local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertNotIn("が作られました", self.errors[0],
                         "確かめられていないのに断定している: %s" % self.errors)
        self.assertIn(".running.cfg.netbelt-part", self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("Permission denied", self.errors[0],
                      "確かめられなかった理由が伝わらない: %s" % self.errors)
        m.sftp_client.posix_rename.assert_not_called()
        self.assertTrue(m.is_connected, "権限エラーで接続まで畳んでいる")

    def test_a_recheck_that_really_finds_a_new_file_still_says_so(self):
        """本当に同名が現れた場合は、これまでどおり作られたと伝えること。"""
        m = self._manager()

        class Attr:
            st_mode = 0o100644

        m.sftp_client.stat.side_effect = [IOError("No such file"), Attr()]

        m.upload_file(self.local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertIn("が作られました", self.errors[0],
                      "同名の出現が伝わらない: %s" % self.errors)
        m.sftp_client.posix_rename.assert_not_called()


if __name__ == "__main__":
    unittest.main()
