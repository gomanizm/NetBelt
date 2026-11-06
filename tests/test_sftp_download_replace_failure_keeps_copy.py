# -*- coding: utf-8 -*-
"""ダウンロードの最終の置き換えに失敗しても、落とし終えた写しを消さないことを検証する。

download_file は一時名へ落とし終えてから os.replace（上書きを承認したとき）
または os.rename（承認していないとき）で最終名へ置き換える。これまで一時名を
残すのは os.rename の FileExistsError だけで、それ以外の OSError（Windows で
保存先を別のプロセスが開いている・読み取り専用属性が付いている、などの
PermissionError [WinError 5]）では、唯一の完全な写しである一時名を消していた。
利用者には WinError のパスだけが届き、内容が失われたことも分からなかった。

置き換えに失敗したら一時名を残し、その場所を知らせること。
"""
import glob
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

OLD = b"old config\n"
NEW = b"new config\n"


class DownloadReplaceFailureKeepsCopyTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.sftp_manager import SFTPManager
        self.dir = tempfile.mkdtemp(prefix="netbelt-dl-replace-fail-")
        self.addCleanup(self._cleanup_dir)
        self.target = os.path.join(self.dir, "backup.cfg")
        m = self.m = SFTPManager()
        m.is_connected = True
        m.current_path = "/flash"
        m.sftp_client = mock.Mock()
        m.sftp_client.get_channel.return_value.closed = False

        def fake_get(remote, local, callback=None, **kwargs):
            with open(local, "wb") as f:
                f.write(NEW)
        m.sftp_client.get.side_effect = fake_get
        self.done, self.errors = [], []
        m.transfer_complete.connect(self.done.append)
        m.error_occurred.connect(self.errors.append)
        type(self)._keep.append(m)

    def _cleanup_dir(self):
        for path in glob.glob(os.path.join(self.dir, "*")):
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        shutil.rmtree(self.dir, True)

    def _pump(self, check, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _parts(self):
        return glob.glob(os.path.join(self.dir, "*.netbelt-part"))

    def _assert_copy_kept_and_named(self):
        self.assertTrue(self._pump(lambda: self.errors or self.done),
                        "ダウンロードの結果が届かない")
        self.assertEqual(self.done, [], "失敗したのに完了と知らせた")
        parts = self._parts()
        self.assertEqual(len(parts), 1,
                         "落とし終えた一時名の写しが残っていない: %s"
                         % os.listdir(self.dir))
        with open(parts[0], "rb") as f:
            self.assertEqual(f.read(), NEW, "残した写しの中身が違う")
        self.assertEqual(len(self.errors), 1, self.errors)
        self.assertIn(parts[0], self.errors[0],
                      "写しを残した場所が利用者に伝わらない: %s" % self.errors)

    def test_a_replace_refused_by_an_open_target_keeps_the_copy(self):
        """保存先を別のハンドルが開いたまま（エディタなど）でも写しを残すこと。"""
        with open(self.target, "wb") as f:
            f.write(OLD)
        # Windows の open は FILE_SHARE_DELETE を付けないので、開いている間は
        # MoveFileEx が ERROR_ACCESS_DENIED で断る
        held = open(self.target, "rb")
        self.addCleanup(held.close)

        self.m.download_file("/flash/backup.cfg", self.target, overwrite=True)

        self._assert_copy_kept_and_named()
        held.close()
        with open(self.target, "rb") as f:
            self.assertEqual(f.read(), OLD, "既存の保存先が変わった")

    def test_a_replace_refused_by_a_read_only_target_keeps_the_copy(self):
        """読み取り専用属性の保存先へ置き換えられなくても写しを残すこと。"""
        with open(self.target, "wb") as f:
            f.write(OLD)
        os.chmod(self.target, stat.S_IREAD)

        self.m.download_file("/flash/backup.cfg", self.target, overwrite=True)

        self._assert_copy_kept_and_named()

    def test_a_rename_refused_for_another_reason_keeps_the_copy(self):
        """承認なしの os.rename が FileExistsError 以外で断られても写しを残すこと。"""
        with mock.patch("core.sftp_manager.os.rename",
                        side_effect=PermissionError(13, "Access is denied")):
            self.m.download_file("/flash/backup.cfg", self.target,
                                 overwrite=False)
            self._assert_copy_kept_and_named()
        self.assertFalse(os.path.exists(self.target))

    def test_a_successful_download_leaves_no_temporary_copy(self):
        """置き換えに成功したときは、これまでどおり一時名を残さないこと。"""
        self.m.download_file("/flash/backup.cfg", self.target, overwrite=False)

        self.assertTrue(self._pump(lambda: self.errors or self.done))
        self.assertEqual(self.errors, [])
        self.assertEqual(self._parts(), [])
        with open(self.target, "rb") as f:
            self.assertEqual(f.read(), NEW)


if __name__ == "__main__":
    unittest.main()
