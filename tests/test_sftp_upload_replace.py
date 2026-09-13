"""アップロードの一時名と、最終名への置き換えの安全性を検証する。

upload_file は一時名へ送ってから最終名へ改名する。その一時名と置き換えの
手順に、外部レビューで 4 件の指摘があった。

- 一時名が最終名から機械的に決まる固定名で、前回の失敗が残した「唯一の
  完全な写し」を次の試行が黙って上書きし、その試行が失敗すると消す
- 改名で置き換えると、既存ファイルの mode が引き継がれず、サーバ既定の
  緩い権限になる
- overwrite=False でも、送る直前の確認から改名までの間に現れた同名を
  posix_rename が確認なしで潰す
- ルート直下（'/name'）への送信で一時名が相対パスになり、サーバの開始
  ディレクトリ側へ書かれる
"""
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpUploadReplaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-replace-")
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _manager(self):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.sftp_client.normalize.side_effect = lambda p: p
        # リモートに同名は無い（stat が失敗する）
        m.sftp_client.stat.side_effect = IOError("No such file")
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

    def _put_targets(self, m):
        return [c[0][1] for c in m.sftp_client.put.call_args_list]

    def _removed(self, m):
        return [c[0][0] for c in m.sftp_client.remove.call_args_list]

    # --- 一時名の一意化 ---

    def test_each_upload_uses_its_own_temporary_name(self):
        """同じ最終名へ 2 回送っても、一時名を使い回さないこと。"""
        m = self._manager()

        m.upload_file(self.local, "/flash/running.cfg", overwrite=True)
        self.assertTrue(self._wait(lambda: self.done), "1 回目が終わらない: %s" % self.errors)
        m.upload_file(self.local, "/flash/running.cfg", overwrite=True)
        self.assertTrue(self._wait(lambda: len(self.done) >= 2),
                        "2 回目が終わらない: %s" % self.errors)

        first, second = self._put_targets(m)
        self.assertNotEqual(first, second,
                            "同じ一時名を使い回している: %s" % first)

    def test_a_failed_upload_does_not_remove_an_earlier_attempts_copy(self):
        """前回の失敗が残した一時名を、次の試行の後始末が消さないこと。"""
        m = self._manager()
        # 1 回目: 最終名を消したあと改名に失敗し、一時名が唯一の完全な写しになる
        m.sftp_client.posix_rename.side_effect = IOError("Operation unsupported")
        m.sftp_client.rename.side_effect = [IOError("Failure"),
                                            IOError("connection lost")]
        m.upload_file(self.local, "/flash/running.cfg", overwrite=True)
        self.assertTrue(self._wait(lambda: self.errors), "1 回目の失敗が通知されない")
        kept = self._put_targets(m)[0]

        # 2 回目: 転送そのものが失敗し、後始末が走る
        m.sftp_client.put.side_effect = IOError("connection lost")
        m.upload_file(self.local, "/flash/running.cfg", overwrite=True)
        self.assertTrue(self._wait(lambda: len(self.errors) >= 2),
                        "2 回目の失敗が通知されない")

        self.assertNotIn(kept, self._removed(m),
                         "前回の試行が残した唯一の完全な写しを消している")


if __name__ == "__main__":
    unittest.main()
