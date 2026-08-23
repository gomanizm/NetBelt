"""SFTP サーバの open() が SFTP のフラグを尊重することを確認する。

書き込み系をすべて 'wb'（常に truncate）で開いていると、
読み書き両用で既存ファイルを開いた瞬間に中身が消え、部分書き換えを行う
クライアントはデータを失う。O_EXCL（新規作成、存在したら失敗）も
黙って上書きしてしまう。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

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


class SftpOpenFlagsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

        cls._fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                      return_value=(True, "test stub"))
        cls._fw.start()
        cls._home = unittest.mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        cls._home.start()

        cls.root = tempfile.mkdtemp(prefix="netbelt-openflags-")
        cls.port = free_port()

        from core.sftp_server import SFTPServerManager
        cls.server = SFTPServerManager()
        assert cls.server.start(port=cls.port, root_dir=cls.root,
                                username=USER, password=PASSWORD)
        deadline = time.time() + 15
        while not cls.server.is_running and time.time() < deadline:
            time.sleep(0.05)
        assert cls.server.is_running

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._fw.stop()
        cls._home.stop()

    def sftp(self):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                  look_for_keys=False, allow_agent=False, timeout=10)
        self.addCleanup(c.close)
        return c.open_sftp()

    def real(self, *parts):
        return os.path.join(self.root, *parts)

    def _seed(self, name, content=b"ORIGINAL-CONTENT"):
        with open(self.real(name), "wb") as f:
            f.write(content)
        return content

    def test_read_write_open_does_not_truncate(self):
        """'r+' で開いただけで既存の中身が消えないこと。"""
        original = self._seed("rw.cfg")
        s = self.sftp()
        fh = s.open("rw.cfg", "r+")
        fh.close()
        with open(self.real("rw.cfg"), "rb") as f:
            self.assertEqual(f.read(), original,
                             "読み書き両用で開いた瞬間に中身が消えた")

    def test_read_write_partial_update_keeps_tail(self):
        """'r+' で先頭を書き換えても、後ろが残ること。"""
        self._seed("patch.cfg", b"AAAABBBBCCCC")
        s = self.sftp()
        with s.open("patch.cfg", "r+") as fh:
            fh.write(b"ZZZZ")
        with open(self.real("patch.cfg"), "rb") as f:
            self.assertEqual(f.read(), b"ZZZZBBBBCCCC")

    def test_exclusive_create_fails_on_existing_file(self):
        """'x'（O_EXCL）は既存ファイルに対して失敗すること。"""
        self._seed("excl.cfg", b"KEEP-ME")
        s = self.sftp()
        with self.assertRaises(IOError):
            s.open("excl.cfg", "x")
        with open(self.real("excl.cfg"), "rb") as f:
            self.assertEqual(f.read(), b"KEEP-ME", "O_EXCL なのに上書きされた")

    def test_exclusive_create_succeeds_on_new_file(self):
        # paramiko の "x" は CREATE|EXCL のみで WRITE を立てないため、
        # 書き込みも行うなら "wx" を指定する
        s = self.sftp()
        with s.open("brand-new.cfg", "wx") as fh:
            fh.write(b"new")
        self.assertTrue(os.path.isfile(self.real("brand-new.cfg")))

    def test_append_keeps_existing_content(self):
        self._seed("app.cfg", b"HEAD")
        s = self.sftp()
        with s.open("app.cfg", "a") as fh:
            fh.write(b"-TAIL")
        with open(self.real("app.cfg"), "rb") as f:
            self.assertEqual(f.read(), b"HEAD-TAIL")

    def test_write_mode_still_truncates(self):
        """'w' はこれまでどおり truncate すること（既存の挙動）。"""
        self._seed("trunc.cfg", b"OLD-LONG-CONTENT")
        s = self.sftp()
        with s.open("trunc.cfg", "w") as fh:
            fh.write(b"new")
        with open(self.real("trunc.cfg"), "rb") as f:
            self.assertEqual(f.read(), b"new")

    def test_read_of_missing_path_creates_no_directory(self):
        """読み取り目的の open で、存在しないディレクトリを作らないこと。"""
        s = self.sftp()
        with self.assertRaises(IOError):
            s.open("no-such-dir/file.cfg", "r")
        self.assertFalse(os.path.exists(self.real("no-such-dir")),
                         "読み取りなのにディレクトリが作られた")

    def test_upload_and_download_still_work(self):
        """通常の put / get を壊していないこと。"""
        s = self.sftp()
        with s.open("roundtrip.bin", "w") as fh:
            fh.write(b"\x00\x01\x02NETBELT")
        with s.open("roundtrip.bin", "r") as fh:
            self.assertEqual(fh.read(), b"\x00\x01\x02NETBELT")


if __name__ == "__main__":
    unittest.main()
