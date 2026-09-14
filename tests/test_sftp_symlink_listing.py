"""ディレクトリへのシンボリックリンクが一覧でディレクトリとして扱われることを検証する。

listdir_attr が返す st_mode は lstat 相当（リンク自身）なので、そのまま
S_ISDIR に掛けるとリンクは常にファイルになる。パネルは is_dir を見てから
change_directory を呼ぶため、ディレクトリへのリンクをダブルクリックしても
無反応で、コンテキストメニューも「開く」ではなく「ダウンロード」になる。

リンクの項目だけ追跡先を stat で引き直す。往復が増えるのはリンクの数だけに
限り、引けないものは従来どおりファイル扱いにすること。
"""
import os
import stat
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class FakeAttr:
    """listdir_attr が返す項目の代わり（必要な属性だけ持つ）。"""

    def __init__(self, filename, st_mode, st_size=0, st_mtime=0):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = st_size
        self.st_mtime = st_mtime


class SftpSymlinkListingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, box, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if box:
                return True
            time.sleep(0.02)
        return False

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.sftp_client.listdir_attr.return_value = [
            FakeAttr("run.cfg", stat.S_IFREG | 0o644, st_size=11),
            FakeAttr("subdir", stat.S_IFDIR | 0o755),
            FakeAttr("dirlink", stat.S_IFLNK | 0o777),
            FakeAttr("filelink", stat.S_IFLNK | 0o777),
            FakeAttr("deadlink", stat.S_IFLNK | 0o777),
        ]

        def follow(path):
            if path == "/base/dirlink":
                return FakeAttr("dirlink", stat.S_IFDIR | 0o755)
            if path == "/base/filelink":
                return FakeAttr("filelink", stat.S_IFREG | 0o644, st_size=7)
            raise IOError("No such file")

        m.sftp_client.stat.side_effect = follow
        return m

    def test_a_link_to_a_directory_is_listed_as_a_directory(self):
        m = self._manager()
        box = []
        m.file_list_ready.connect(box.append)
        m.list_directory("/base")
        self.assertTrue(self._wait(box), "file_list_ready が来なかった")

        by_name = {e["name"]: e for e in box[0]}
        self.assertTrue(by_name["dirlink"]["is_dir"],
                        "ディレクトリへのリンクがファイル扱いのまま")
        self.assertFalse(by_name["filelink"]["is_dir"])
        self.assertFalse(by_name["deadlink"]["is_dir"],
                         "追跡できないリンクはファイル扱いのまま")
        self.assertTrue(by_name["subdir"]["is_dir"])
        self.assertFalse(by_name["run.cfg"]["is_dir"])

    def test_the_permission_column_still_shows_the_link_itself(self):
        m = self._manager()
        box = []
        m.file_list_ready.connect(box.append)
        m.list_directory("/base")
        self.assertTrue(self._wait(box), "file_list_ready が来なかった")

        by_name = {e["name"]: e for e in box[0]}
        self.assertTrue(by_name["dirlink"]["permissions"].startswith("l"),
                        "リンクであることが権限欄から消えた")

    def test_only_links_cost_an_extra_round_trip(self):
        m = self._manager()
        box = []
        m.file_list_ready.connect(box.append)
        m.list_directory("/base")
        self.assertTrue(self._wait(box), "file_list_ready が来なかった")

        asked = [c.args[0] for c in m.sftp_client.stat.call_args_list]
        self.assertEqual(sorted(asked),
                         ["/base/deadlink", "/base/dirlink", "/base/filelink"],
                         "リンク以外にも stat を投げている")


    def test_a_timed_out_link_lookup_stops_the_listing(self):
        """追跡の stat が期限切れしたら、残りのリンクを順に待たないこと。

        リンクの数だけ期限（既定 30 秒）を積み上げると、一覧の更新が
        何分も返らなくなる。期限切れは接続が使えなくなった印なので、
        一覧をやめて失敗として扱う。
        """
        m = self._manager()
        # 失敗すると接続を畳んで sftp_client を手放すので、先に控える
        client = m.sftp_client
        client.stat.side_effect = TimeoutError()
        errors = []
        m.error_occurred.connect(errors.append)

        m.list_directory("/base")
        self.assertTrue(self._wait(errors), "失敗が通知されない")
        self.assertEqual(client.stat.call_count, 1,
                         "期限切れのあとも残りのリンクを stat している")

if __name__ == "__main__":
    unittest.main()
