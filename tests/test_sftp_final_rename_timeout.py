"""remove のあとの3本目の rename が期限切れになったときの扱いを検証する。

posix_rename を持たないサーバ向けの復旧手順は、最終名を remove してから
もう一度 rename する。この3本目の rename が socket.timeout で戻ると、
TimeoutError は OSError（= IOError）でもあるため `except IOError` に捕まり、
「最終名への置き換えに失敗しました」という確定した失敗として報告されていた。
実際には機器側で置き換えが済んでいて応答だけが返らないことがあり、確定した
失敗と読むと利用者は最終名が無事だと誤解する（既に remove してある）。

1本目（posix_rename）・2本目（rename）の期限切れは、確かめられなかった扱い
（unknown_outcome）へ寄せてある。3本目からも同じ扱いに落ちること。
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


class SftpFinalRenameTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-final-rename-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        # 改名の期限切れでは接続を畳んで m.sftp_client を手放すので、
        # 呼び出しの記録は控えたほうで見る
        self.client = m.sftp_client = mock.Mock()
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

    def _upload_with_a_timed_out_final_rename(self, m):
        """3本目の rename だけが期限切れになる機器を作って送る"""
        # posix_rename の無いサーバ。既存があるので1本目の rename は失敗し、
        # remove したあとの2本目（= 経路としては3本目の rename 呼び出し）が
        # 機器側では適用され、応答だけが返らない
        m.sftp_client.posix_rename.side_effect = AttributeError("no posix-rename")
        m.sftp_client.rename.side_effect = [IOError("File exists"), TimeoutError()]
        m.upload_file(self.local, "/flash/running.cfg", overwrite=True)
        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")

    def test_a_timed_out_final_rename_is_not_a_confirmed_failure(self):
        """置き換えの成否は確かめられていない。確定した失敗として報告しないこと。"""
        m = self._manager()
        self._upload_with_a_timed_out_final_rename(m)

        self.assertNotIn("最終名への置き換えに失敗しました", self.errors[0],
                         "確かめられていないのに失敗と断定している: %s" % self.errors)
        self.assertIn("確かめられませんでした", self.errors[0],
                      "確かめられなかったことが伝わらない: %s" % self.errors)

    def test_a_timed_out_final_rename_keeps_the_only_copy(self):
        """転送した唯一の写し（一時名）を後始末で消さず、場所を知らせること。"""
        m = self._manager()
        self._upload_with_a_timed_out_final_rename(m)

        self.assertIn(".running.cfg.netbelt-part", self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(
            removed, ["/flash/running.cfg"],
            "一時名まで消している、または最終名の remove が変わった: %s" % removed)

    def test_a_timed_out_final_rename_says_the_final_name_is_gone(self):
        """最終名は復旧手順で既に消してある。その事実を伝えること。"""
        m = self._manager()
        self._upload_with_a_timed_out_final_rename(m)

        self.assertIn("最終名", self.errors[0],
                      "最終名がどうなっているか伝わらない: %s" % self.errors)
        self.assertIn("消してあります", self.errors[0],
                      "最終名を既に消したことが伝わらない: %s" % self.errors)

    def test_a_timed_out_final_rename_gives_a_reason(self):
        """socket.timeout は str が空。理由の無い「: 」で終わらせないこと。"""
        m = self._manager()
        self._upload_with_a_timed_out_final_rename(m)

        self.assertFalse(self.errors[0].rstrip().endswith(":"),
                         "理由が空のまま終わっている: %s" % self.errors)

    def test_a_final_rename_that_really_fails_is_still_a_failure(self):
        """本当に失敗したとき（期限切れでない）は、これまでどおり失敗と伝えること。"""
        m = self._manager()
        m.sftp_client.posix_rename.side_effect = AttributeError("no posix-rename")
        m.sftp_client.rename.side_effect = [IOError("File exists"),
                                            IOError("Permission denied")]

        m.upload_file(self.local, "/flash/running.cfg", overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertIn("最終名への置き換えに失敗しました", self.errors[0],
                      "確定した失敗が伝わらない: %s" % self.errors)
        self.assertIn(".running.cfg.netbelt-part", self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)


if __name__ == "__main__":
    unittest.main()
