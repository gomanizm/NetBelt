"""ディレクトリへのリンクを、パネルから消せることを検証する。

一覧はリンクの追跡先で is_dir を決める（ダブルクリックでリンクの先へ
入れるようにするため）。その is_dir をそのまま delete_item へ渡すと、
リンク自身に対して rmdir が発行される。POSIX の rmdir はシンボリック
リンクに対して ENOTDIR で失敗するので、パネルからそのリンクを消せない。

移動の可否は追跡先、削除の可否はリンク自身。参照先を分けること。
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


class SftpDirLinkDeleteTest(unittest.TestCase):
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
        ]

        def follow(path):
            if path == "/base/dirlink":
                return FakeAttr("dirlink", stat.S_IFDIR | 0o755)
            if path == "/base/filelink":
                return FakeAttr("filelink", stat.S_IFREG | 0o644, st_size=7)
            raise IOError("No such file")

        m.sftp_client.stat.side_effect = follow
        return m

    def _listing(self, m):
        box = []
        m.file_list_ready.connect(box.append)
        m.list_directory("/base")
        self.assertTrue(self._wait(box), "file_list_ready が来なかった")
        return {e["name"]: e for e in box[0]}

    def test_the_listing_says_which_entries_are_links(self):
        """削除の相手を決められるよう、リンクかどうかを一覧に持たせること。"""
        m = self._manager()
        by_name = self._listing(m)

        self.assertTrue(by_name["dirlink"]["is_link"],
                        "ディレクトリへのリンクがリンクだと分からない")
        self.assertTrue(by_name["filelink"]["is_link"])
        self.assertFalse(by_name["subdir"]["is_link"],
                         "本物のディレクトリをリンク扱いしている")
        self.assertFalse(by_name["run.cfg"]["is_link"])
        # 移動の可否はこれまでどおり追跡先で決める
        self.assertTrue(by_name["dirlink"]["is_dir"],
                        "リンクの先へ入れなくなっている")

    def _panel_for(self, m, entries):
        from ui.sftp_panel import SFTPPanel

        panel = SFTPPanel()
        self.addCleanup(panel.close)
        panel.resize(600, 400)
        panel.show()
        panel.set_sftp_manager(m, "rtrA")
        panel._update_file_list(list(entries))
        self.app.processEvents()
        return panel

    def _delete(self, panel, file_info):
        from PyQt6.QtWidgets import QMessageBox
        from ui import sftp_panel as mod

        with mock.patch.object(mod.QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            panel._on_delete_selected(file_info)
        self.app.processEvents()

    def test_deleting_a_link_to_a_directory_unlinks_it(self):
        """リンク自身を消す。rmdir はリンクに対して必ず失敗する。"""
        m = self._manager()
        by_name = self._listing(m)
        panel = self._panel_for(m, by_name.values())

        self._delete(panel, by_name["dirlink"])

        removed = [c[0][0] for c in m.sftp_client.remove.call_args_list]
        self.assertEqual(removed, ["/base/dirlink"],
                         "リンク自身を unlink していない: %s" % removed)
        m.sftp_client.rmdir.assert_not_called()

    def test_deleting_a_real_directory_still_uses_rmdir(self):
        """本物のディレクトリはこれまでどおり rmdir で消すこと。"""
        m = self._manager()
        by_name = self._listing(m)
        panel = self._panel_for(m, by_name.values())

        self._delete(panel, by_name["subdir"])

        rmdirs = [c[0][0] for c in m.sftp_client.rmdir.call_args_list]
        self.assertEqual(rmdirs, ["/base/subdir"],
                         "ディレクトリが rmdir で消えなくなった: %s" % rmdirs)
        m.sftp_client.remove.assert_not_called()

    def test_deleting_a_plain_file_still_uses_remove(self):
        """ファイルはこれまでどおり remove で消すこと。"""
        m = self._manager()
        by_name = self._listing(m)
        panel = self._panel_for(m, by_name.values())

        self._delete(panel, by_name["run.cfg"])

        removed = [c[0][0] for c in m.sftp_client.remove.call_args_list]
        self.assertEqual(removed, ["/base/run.cfg"])
        m.sftp_client.rmdir.assert_not_called()

    def test_an_entry_without_the_link_flag_is_still_handled(self):
        """is_link を持たない古い形の file_info でも落ちないこと。"""
        m = self._manager()
        self._listing(m)                    # current_path を /base にする
        entry = {"name": "subdir", "size": 0, "mtime": 0, "mode": 0o040755,
                 "is_dir": True, "permissions": "drwxr-xr-x"}
        panel = self._panel_for(m, [entry])

        self._delete(panel, entry)

        rmdirs = [c[0][0] for c in m.sftp_client.rmdir.call_args_list]
        self.assertEqual(rmdirs, ["/base/subdir"])


if __name__ == "__main__":
    unittest.main()
