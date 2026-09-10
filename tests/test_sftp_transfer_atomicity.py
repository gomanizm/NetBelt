"""SFTP の転送が失敗しても、既存のファイルを壊さないことを検証する。

download_file は get() に最終の保存先をそのまま渡していた。paramiko の
get() はリモートを読む前にローカルを 'wb' で開くので、一覧を見たあとに
リモート側でファイルが消えていただけでも、上書き先にあった正常な
バックアップが 0 バイトになる。途中で切れれば部分ファイルが本来の名前で
残る。upload_file も put() で先にリモートを切り詰めるため、切断・容量
不足で機器側に途中までの設定ファイルが残る（いずれも実測で確認）。

一時名へ転送し、成功を確かめてから最終名へ置き換える。失敗したら一時
ファイルを消し、既存のファイルはそのまま残す。
"""
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpTransferAtomicityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-atomic-")

    def _manager(self):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.sftp_client.normalize.side_effect = lambda p: p
        # 転送後の一覧更新は動かさない
        m.list_directory = mock.Mock()
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

    # --- ダウンロード ---

    def test_a_failed_download_keeps_the_existing_local_file(self):
        m = self._manager()
        local = os.path.join(self.dir, "backup.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("known-good backup")

        def get_that_fails(remote, localpath, callback=None):
            with open(localpath, "wb") as f:        # paramiko は先にローカルを開く
                f.write(b"partial")
            raise IOError("remote read failed")

        m.sftp_client.get.side_effect = get_that_fails
        m.download_file("/etc/backup.cfg", local)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        with io.open(local, encoding="utf-8") as f:
            self.assertEqual(f.read(), "known-good backup",
                             "失敗した転送が既存のファイルを壊した")
        self.assertEqual([n for n in os.listdir(self.dir) if n != "backup.cfg"], [],
                         "一時ファイルが残っている: %s" % os.listdir(self.dir))

    def test_a_successful_download_replaces_the_local_file(self):
        m = self._manager()
        local = os.path.join(self.dir, "backup.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("old")

        def get_ok(remote, localpath, callback=None):
            with open(localpath, "wb") as f:
                f.write(b"new content")

        m.sftp_client.get.side_effect = get_ok
        m.download_file("/etc/backup.cfg", local)

        self.assertTrue(self._wait(lambda: self.done), "完了が通知されない: %s" % self.errors)
        with io.open(local, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new content")
        self.assertEqual(os.listdir(self.dir), ["backup.cfg"])

    # --- アップロード ---

    def test_a_failed_upload_does_not_touch_the_remote_final_name(self):
        m = self._manager()
        local = os.path.join(self.dir, "running.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("hostname R1")
        m.sftp_client.put.side_effect = IOError("connection lost")

        m.upload_file(local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        put_target = m.sftp_client.put.call_args[0][1]
        self.assertNotEqual(put_target, "/flash/running.cfg",
                            "最終名へ直接書いている（途中で切れると機器に壊れた設定が残る）")
        self.assertTrue(put_target.startswith("/flash/"), "一時名が別ディレクトリ: %s" % put_target)
        renames = m.sftp_client.posix_rename.call_args_list + m.sftp_client.rename.call_args_list
        self.assertEqual(renames, [], "失敗したのに最終名へ置き換えている")
        m.sftp_client.remove.assert_called_once_with(put_target)

    def test_a_successful_upload_is_moved_into_place(self):
        m = self._manager()
        local = os.path.join(self.dir, "running.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

        m.upload_file(local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.done), "完了が通知されない: %s" % self.errors)
        put_target = m.sftp_client.put.call_args[0][1]
        self.assertNotEqual(put_target, "/flash/running.cfg")
        m.sftp_client.posix_rename.assert_called_once_with(put_target, "/flash/running.cfg")


if __name__ == "__main__":
    unittest.main()
