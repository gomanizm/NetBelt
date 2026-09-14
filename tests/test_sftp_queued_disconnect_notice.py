"""順番待ちの間に切断された操作が、黙って消えないことを検証する。

upload_file / list_directory はワーカースレッドを起こし、そのスレッドが
_sftp_lock を取ってから接続をもう一度確かめる。そこで切断済みだと
`return` するだけなので、完了通知もエラー通知も出ないまま要求が消える。
利用者から見ると、ドロップしたファイルが送られたのかどうかも、一覧が
更新されないのが何故かも分からない。

download_file 側は同じ穴を IOError の送出へ直してあり、既存の except /
_fail 経路が通知を出す。upload と list も同じ形にすること。
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


class SftpQueuedDisconnectNoticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-queued-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        self.errors, self.done, self.listed = [], [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        m.file_list_ready.connect(self.listed.append)
        return m

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_an_upload_cancelled_while_queued_tells_the_user(self):
        """順番待ちのアップロードが切断されたら、理由を通知すること。"""
        m = self._manager()

        # 先行する転送がロックを握っている状態を作る
        m._sftp_lock.acquire()
        try:
            m.upload_file(self.local, "/flash/running.cfg", overwrite=True)
            # 順番待ちのあいだに切断される
            m.is_connected = False
        finally:
            m._sftp_lock.release()

        self.assertTrue(
            self._wait(lambda: self.errors or self.done),
            "通知が一切出ない（利用者は送られたかどうか分からない）")
        self.assertEqual(self.done, [], "送っていないのに完了と通知している")
        self.assertIn("接続", self.errors[0],
                      "切断が理由だと伝わらない: %s" % self.errors)
        m.sftp_client.put.assert_not_called()

    def test_a_listing_cancelled_while_queued_tells_the_user(self):
        """順番待ちの一覧取得が切断されたら、理由を通知すること。"""
        m = self._manager()

        m._sftp_lock.acquire()
        try:
            m.list_directory("/flash")
            m.is_connected = False
        finally:
            m._sftp_lock.release()

        self.assertTrue(
            self._wait(lambda: self.errors or self.listed),
            "通知が一切出ない（一覧が変わらない理由が分からない）")
        self.assertEqual(self.listed, [], "取れていないのに一覧を配っている")
        self.assertIn("接続", self.errors[0],
                      "切断が理由だと伝わらない: %s" % self.errors)
        m.sftp_client.listdir_attr.assert_not_called()

    def test_an_upload_cancelled_while_queued_leaves_no_remote_temp_name(self):
        """送る前に切断されたのだから、機器へは何も触らないこと。"""
        m = self._manager()

        m._sftp_lock.acquire()
        try:
            m.upload_file(self.local, "/flash/running.cfg", overwrite=True)
            m.is_connected = False
        finally:
            m._sftp_lock.release()

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        m.sftp_client.remove.assert_not_called()
        m.sftp_client.posix_rename.assert_not_called()
        m.sftp_client.rename.assert_not_called()


if __name__ == "__main__":
    unittest.main()
