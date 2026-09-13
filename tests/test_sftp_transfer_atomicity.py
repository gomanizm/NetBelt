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
        # リモートに同名は無い（stat が失敗する）。upload_file は overwrite を
        # 明示されない限り、送る直前に stat で既存を確かめるようになった
        m.sftp_client.stat.side_effect = IOError("No such file")
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

    def test_a_relative_remote_name_keeps_its_temporary_name_relative(self):
        """スラッシュを含まないリモート名では、一時名をルート直下にしないこと。"""
        m = self._manager()
        local = os.path.join(self.dir, "running.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

        m.upload_file(local, "running.cfg")

        self.assertTrue(self._wait(lambda: self.done or self.errors), "終わらない")
        put_target = m.sftp_client.put.call_args[0][1]
        self.assertFalse(put_target.startswith("/"),
                         "相対名なのに一時名が絶対パス: %s" % put_target)

    def test_two_downloads_to_the_same_local_path_both_succeed(self):
        """同じ保存先へ続けて落としても、片方が誤って失敗にならないこと。"""
        m = self._manager()
        local = os.path.join(self.dir, "backup.cfg")

        def get_ok(remote, localpath, callback=None):
            time.sleep(0.05)
            with open(localpath, "wb") as f:
                f.write(remote.encode())

        m.sftp_client.get.side_effect = get_ok
        m.download_file("/etc/one.cfg", local)
        m.download_file("/etc/two.cfg", local)

        self.assertTrue(self._wait(lambda: len(self.done) + len(self.errors) >= 2))
        self.assertEqual(self.errors, [], "同じ一時名の取り合いで誤って失敗している")
        self.assertEqual(os.listdir(self.dir), ["backup.cfg"], "一時ファイルが残っている")

    def test_the_fallback_renames_first_and_removes_only_when_needed(self):
        """posix_rename が無いサーバでは、まず rename を試すこと。

        先に最終名を消してから rename すると、rename が失敗した瞬間に
        機器側の元ファイルが消える。
        """
        m = self._manager()
        local = os.path.join(self.dir, "running.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("hostname R1")
        m.sftp_client.posix_rename.side_effect = IOError("Operation unsupported")

        m.upload_file(local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.done), "完了しない: %s" % self.errors)
        m.sftp_client.rename.assert_called_once()
        m.sftp_client.remove.assert_not_called()

    def test_a_failed_rename_after_removing_the_target_keeps_the_temporary_copy(self):
        """最終名を消したあと rename に失敗したら、唯一の完全な写しである一時名を消さず、名前を知らせること。"""
        m = self._manager()
        local = os.path.join(self.dir, "running.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("hostname R1")
        m.sftp_client.posix_rename.side_effect = IOError("Operation unsupported")
        # 1 回目の rename は「既にある」で失敗、remove 後の 2 回目は切断で失敗
        m.sftp_client.rename.side_effect = [IOError("Failure"), IOError("connection lost")]

        m.upload_file(local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        removed = [c[0][0] for c in m.sftp_client.remove.call_args_list]
        self.assertNotIn("/flash/.running.cfg.netbelt-part", removed,
                         "唯一の完全な写しを消している")
        self.assertIn(".running.cfg.netbelt-part", self.errors[0],
                     "機器に残った一時名を知らせていない: %s" % self.errors)

    def test_a_rename_that_times_out_removes_neither_name(self):
        """置き換えの応答が期限切れになったら、どちらの名前も消さないこと。

        posix_rename が機器側では適用され、応答だけが返らない場合がある。
        socket.timeout（= TimeoutError）は IOError でもあるので、これまでは
        「posix_rename が使えないサーバ」と同じ後始末へ落ちていた。一時名は
        既に無いので rename が失敗し、その復旧として最終名を remove する。
        置き換わったばかりの内容と、転送した写しの両方が消える。
        """
        m = self._manager()
        local = os.path.join(self.dir, "running.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("hostname R1")
        m.sftp_client.posix_rename.side_effect = TimeoutError()
        # 機器側では置き換わっているので、一時名はもう無い
        m.sftp_client.rename.side_effect = IOError("No such file")

        m.upload_file(local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        removed = [c[0][0] for c in m.sftp_client.remove.call_args_list]
        self.assertEqual(removed, [], "期限切れなのに消しにいっている: %s" % removed)
        m.sftp_client.rename.assert_not_called()
        self.assertIn(".running.cfg.netbelt-part", self.errors[0],
                      "機器側で確かめる一時名を知らせていない: %s" % self.errors)

    def test_a_fallback_rename_that_times_out_removes_neither_name(self):
        """posix_rename の無いサーバで、代わりの rename が期限切れになった場合も同じ。"""
        m = self._manager()
        local = os.path.join(self.dir, "running.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("hostname R1")
        m.sftp_client.posix_rename.side_effect = IOError("Operation unsupported")
        m.sftp_client.rename.side_effect = TimeoutError()

        m.upload_file(local, "/flash/running.cfg")

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        removed = [c[0][0] for c in m.sftp_client.remove.call_args_list]
        self.assertEqual(removed, [], "期限切れなのに消しにいっている: %s" % removed)
        self.assertEqual(m.sftp_client.rename.call_count, 1,
                         "期限切れのあとに rename をやり直している")

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


    def test_a_download_into_a_missing_directory_is_reported_not_raised(self):
        """保存先に一時ファイルを作れないときは、例外ではなく通知で知らせること。

        download_file は GUI スレッドから呼ばれる。一時ファイルの作成が
        そこで例外を上げると、転送の失敗ではなくスロットの例外になる。
        """
        m = self._manager()
        local = os.path.join(self.dir, "no-such-dir", "backup.cfg")
        try:
            m.download_file("/etc/backup.cfg", local)
        except OSError as e:
            self.fail("download_file が例外を上げた: %r" % e)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertIn("ダウンロードエラー", self.errors[0])
        m.sftp_client.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
