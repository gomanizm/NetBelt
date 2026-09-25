"""上書きの確認を経ていない送信が、posix_rename で既存を黙って潰さないことを検証する。

最終名への置き換えは overwrite を見ずに posix_rename（OpenSSH 拡張）から
試していた。posix_rename は「既存があれば上書きする」改名なので、送る直前の
確認が既存を見落とした機器では、確認を経ていない送信がそのまま既存を潰す。
STAT を実装せず、在るファイルにも汎用の失敗を返す機器では _remote_probe が
「無い」と読むため、この経路は現実に届く。

実測（mock の機器: stat が IOError("Failure")、posix_rename が成功、
overwrite=False）: posix_rename が最終名へ呼ばれ、errors=[]、
done=['アップロード完了: running.cfg'] だった。より現実的なのは
_remote_probe 自身が認めている TOCTOU（転送後の再確認から改名までの窓）で、
第三者がその窓で作った同名を潰す。

直し方（利用者の決定 2026-09-20）: overwrite が真のときだけ posix_rename を
使い、確認を経ていない送信は最初から「既存があれば失敗する」rename を使う。
rename が断られたら、既存の「上書きの確認を経ていないので最終名には触れて
いません」の枝がそのまま効く。
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


class SftpUnconfirmedPosixRenameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-unconfirmed-posix-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        # STAT を実装しない機器。在るファイルにも汎用の失敗を返すので、
        # 送る直前の確認も転送後の再確認も「無い」と読む
        self.client.stat.side_effect = IOError("Failure")
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

    def test_an_unconfirmed_upload_never_uses_posix_rename(self):
        """確認を経ていない送信は、既存を上書きする改名を使わないこと。"""
        m = self._manager()

        m.upload_file(self.local, FINAL)

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.client.posix_rename.assert_not_called()
        tmp = self.client.put.call_args[0][1]
        self.client.rename.assert_called_once_with(tmp, FINAL)

    def test_an_unconfirmed_upload_is_refused_when_the_name_already_exists(self):
        """「既存があれば失敗する」改名が断ったら、最終名に触れずに知らせること。"""
        m = self._manager()
        # 最終名は実在する。非 posix の rename はそれを理由に断る
        self.client.rename.side_effect = IOError("Failure")

        m.upload_file(self.local, FINAL)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertEqual(self.done, [],
                         "置き換えていないのに完了を通知している: %s" % self.done)
        self.assertEqual(self.client.rename.call_count, 1,
                         "最終名を消す復旧手順へ進んでいる")
        tmp = self.client.put.call_args[0][1]
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [],
                         "確認を経ていない送信が消しにいっている: %s" % removed)
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("最終名には触れていません", self.errors[0],
                      "最終名が無事であることが伝わらない: %s" % self.errors)

    def test_a_confirmed_overwrite_still_uses_posix_rename(self):
        """上書きを承認済みなら、これまでどおり posix_rename で置き換えること。"""
        m = self._manager()

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.done), "完了しない: %s" % self.errors)
        tmp = self.client.put.call_args[0][1]
        self.client.posix_rename.assert_called_once_with(tmp, FINAL)
        self.client.rename.assert_not_called()


if __name__ == "__main__":
    unittest.main()
