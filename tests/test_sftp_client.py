"""SFTPManager (SFTP クライアント) の統合テスト。

相手はアプリ自身の SFTPServerManager。クライアント単体だけでなく、
自前サーバとの噛み合わせも同時に検証する。

list_directory / upload_file / download_file はバックグラウンドスレッドで動き
シグナルで結果を返すため、Qt のイベントループを回しながら待つ。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
import unittest.mock

import paramiko

sys.path.insert(0, "src")

USER = "netbelt"
PASSWORD = "test-password"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SftpClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

        # 無人テストで実ファイアウォールを叩かないようスタブする
        cls._fw_patch = unittest.mock.patch(
            "core.firewall.ensure_inbound_allow", return_value=(True, "test stub"))
        cls._fw_patch.start()

        # ホストキーの保存先をユーザーのホームから隔離する
        import tempfile
        from pathlib import Path
        cls._data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        cls._appdir_patch = unittest.mock.patch(
            "core.config_manager.app_data_dir", return_value=cls._data_dir)
        cls._appdir_patch.start()

        cls.root = tempfile.mkdtemp(prefix="netbelt-sftp-cli-")
        cls.port = free_port()

        from core.sftp_server import SFTPServerManager
        cls.server = SFTPServerManager()          # RSA 鍵生成でここが遅い
        assert cls.server.start(port=cls.port, root_dir=cls.root,
                                username=USER, password=PASSWORD)
        deadline = time.time() + 15
        while not cls.server.is_running and time.time() < deadline:
            time.sleep(0.05)
        assert cls.server.is_running, "SFTP サーバが待ち受け状態にならなかった"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._fw_patch.stop()
        cls._appdir_patch.stop()

    # --- 補助 ---

    def manager(self, connect=True):
        """SFTPManager を作る。connect=True なら自前サーバへ繋いだ状態で返す。"""
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        if not connect:
            return m
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                    look_for_keys=False, allow_agent=False, timeout=10)
        self.addCleanup(ssh.close)
        self.assertTrue(m.connect(ssh), "SFTP セッションを開けなかった")
        return m

    def collect(self, signal):
        """シグナルの引数を溜める箱を返す。"""
        box = []
        signal.connect(lambda *args: box.append(args[0] if len(args) == 1 else args))
        return box

    def wait(self, box, timeout=15, what="シグナル"):
        """Qt のイベントループを回しつつ box に何か入るのを待つ。"""
        from PyQt6.QtWidgets import QApplication
        deadline = time.time() + timeout
        while not box and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        self.assertTrue(box, "%s が来なかった (timeout=%ss)" % (what, timeout))
        return box

    def real(self, *parts):
        return os.path.join(self.root, *parts)

    def local_tmp(self):
        d = tempfile.mkdtemp(prefix="netbelt-sftp-local-")
        return d

    # --- 接続 ---

    def test_connect_emits_connected(self):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        box = self.collect(m.connected)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                    look_for_keys=False, allow_agent=False, timeout=10)
        self.addCleanup(ssh.close)
        self.assertTrue(m.connect(ssh))
        self.assertTrue(m.is_connected)
        self.assertEqual(len(box), 1)

    def test_connect_with_none_client_fails(self):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        errors = self.collect(m.error_occurred)
        self.assertFalse(m.connect(None))
        self.assertFalse(m.is_connected)
        self.assertTrue(errors)

    def test_disconnect_emits_disconnected(self):
        m = self.manager()
        box = self.collect(m.disconnected)
        m.disconnect()
        self.assertFalse(m.is_connected)
        self.assertEqual(len(box), 1)

    # --- 一覧 ---

    def test_list_directory_reports_files_and_dirs(self):
        os.makedirs(self.real("listdir-case", "subdir"))
        with open(self.real("listdir-case", "run.cfg"), "w", encoding="utf-8") as f:
            f.write("hostname R1")

        m = self.manager()
        box = self.collect(m.file_list_ready)
        m.list_directory("/listdir-case")
        entries = self.wait(box, what="file_list_ready")[0]

        by_name = {e["name"]: e for e in entries}
        self.assertIn("run.cfg", by_name)
        self.assertIn("subdir", by_name)
        self.assertFalse(by_name["run.cfg"]["is_dir"])
        self.assertTrue(by_name["subdir"]["is_dir"])
        self.assertEqual(by_name["run.cfg"]["size"], len("hostname R1"))
        # ディレクトリが先に並ぶ
        self.assertEqual(entries[0]["name"], "subdir")

    def test_list_directory_without_connection_emits_error(self):
        m = self.manager(connect=False)
        errors = self.collect(m.error_occurred)
        m.list_directory("/")
        self.assertTrue(errors)

    # --- 転送 ---

    def test_upload_file_puts_file_on_server(self):
        local_dir = self.local_tmp()
        local = os.path.join(local_dir, "upload.cfg")
        with open(local, "w", encoding="utf-8") as f:
            f.write("hostname UPLOADED")

        m = self.manager()
        done = self.collect(m.transfer_complete)
        m.upload_file(local, "/upload.cfg")
        self.wait(done, what="transfer_complete(upload)")

        self.assertTrue(os.path.isfile(self.real("upload.cfg")))
        with open(self.real("upload.cfg"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "hostname UPLOADED")

    def test_upload_file_emits_progress(self):
        local_dir = self.local_tmp()
        local = os.path.join(local_dir, "big.bin")
        with open(local, "wb") as f:
            f.write(b"N" * 200000)

        m = self.manager()
        progress = self.collect(m.transfer_progress)
        done = self.collect(m.transfer_complete)
        m.upload_file(local, "/big.bin")
        self.wait(done, what="transfer_complete(upload)")
        self.assertTrue(progress, "transfer_progress が一度も来なかった")
        sent, total = progress[-1]
        self.assertEqual(total, 200000)
        self.assertEqual(sent, 200000)

    def test_download_file_fetches_contents(self):
        with open(self.real("download.cfg"), "w", encoding="utf-8") as f:
            f.write("hostname DOWNLOADED")
        local = os.path.join(self.local_tmp(), "fetched.cfg")

        m = self.manager()
        done = self.collect(m.transfer_complete)
        m.download_file("/download.cfg", local)
        self.wait(done, what="transfer_complete(download)")

        with open(local, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hostname DOWNLOADED")

    def test_upload_without_connection_emits_error(self):
        m = self.manager(connect=False)
        errors = self.collect(m.error_occurred)
        m.upload_file("nonexistent.cfg", "/x.cfg")
        self.assertTrue(errors)

    # --- ディレクトリ操作 ---

    def test_create_directory(self):
        m = self.manager()
        m.create_directory("/created-dir")
        self.assertTrue(os.path.isdir(self.real("created-dir")))

    def test_rename_item(self):
        with open(self.real("before.cfg"), "w", encoding="utf-8") as f:
            f.write("x")
        m = self.manager()
        m.rename_item("/before.cfg", "/after.cfg")
        self.assertFalse(os.path.exists(self.real("before.cfg")))
        self.assertTrue(os.path.isfile(self.real("after.cfg")))

    def test_delete_file(self):
        with open(self.real("delete-me.cfg"), "w", encoding="utf-8") as f:
            f.write("x")
        m = self.manager()
        m.delete_item("/delete-me.cfg", is_dir=False)
        self.assertFalse(os.path.exists(self.real("delete-me.cfg")))

    def test_delete_directory(self):
        os.makedirs(self.real("delete-me-dir"))
        m = self.manager()
        m.delete_item("/delete-me-dir", is_dir=True)
        self.assertFalse(os.path.exists(self.real("delete-me-dir")))

    def test_delete_without_connection_emits_error(self):
        m = self.manager(connect=False)
        errors = self.collect(m.error_occurred)
        m.delete_item("/whatever.cfg")
        self.assertTrue(errors)

    # --- パス操作 ---

    def test_change_directory_updates_current_path(self):
        os.makedirs(self.real("chdir-case"), exist_ok=True)
        m = self.manager()
        box = self.collect(m.file_list_ready)
        m.change_directory("/chdir-case")
        self.wait(box, what="file_list_ready")
        self.assertEqual(m.get_current_path(), "/chdir-case")

    def test_get_parent_directory(self):
        m = self.manager(connect=False)
        m.current_path = "/a/b/c"
        self.assertEqual(m.get_parent_directory(), "/a/b")
        m.current_path = "/"
        self.assertEqual(m.get_parent_directory(), "/",
                         "ルートより上へは行かせない")

    # --- 純粋関数 ---

    def test_is_directory(self):
        import stat
        from core.sftp_manager import SFTPManager
        self.assertTrue(SFTPManager._is_directory(stat.S_IFDIR | 0o755))
        self.assertFalse(SFTPManager._is_directory(stat.S_IFREG | 0o644))

    def test_format_permissions(self):
        import stat
        from core.sftp_manager import SFTPManager
        self.assertEqual(SFTPManager._format_permissions(stat.S_IFREG | 0o755),
                         "-rwxr-xr-x")
        self.assertEqual(SFTPManager._format_permissions(stat.S_IFDIR | 0o755),
                         "drwxr-xr-x")
        self.assertEqual(SFTPManager._format_permissions(stat.S_IFREG | 0o644),
                         "-rw-r--r--")


if __name__ == "__main__":
    unittest.main()
