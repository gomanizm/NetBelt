"""SFTP サーバの SETSTAT が、受け取った属性をファイルへ反映することを確認する。

chattr は st_mode だけを見て os.chmod を呼び、サイズ（truncate）と
日時（utime）を黙って捨てたまま SFTP_OK を返していた。クライアントには
成功として見えるので、`sftp -p` の日時保持や truncate に依存する同期は
エラーも出ないまま効かない。
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


class SftpServerSetstatTest(unittest.TestCase):
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

        cls.root = tempfile.mkdtemp(prefix="netbelt-setstat-")
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

    def _seed(self, name, content=b"0123456789" * 3):
        with open(self.real(name), "wb") as f:
            f.write(content)
        return content

    def test_truncate_shortens_the_file(self):
        """truncate 要求でファイルが縮むこと（成功と答えて無視しない）。"""
        self._seed("trunc.cfg")
        s = self.sftp()
        s.truncate("trunc.cfg", 0)
        self.assertEqual(os.path.getsize(self.real("trunc.cfg")), 0,
                         "truncate が成功と答えたのに反映されていない")

    def test_truncate_keeps_the_head_of_the_file(self):
        """縮めるときは先頭から指定バイトを残すこと。"""
        content = self._seed("head.cfg")
        s = self.sftp()
        s.truncate("head.cfg", 4)
        with open(self.real("head.cfg"), "rb") as f:
            self.assertEqual(f.read(), content[:4])

    def test_utime_sets_the_modification_time(self):
        """日時保持（sftp -p）の要求が反映されること。"""
        self._seed("times.cfg")
        s = self.sftp()
        stamp = 1000000000          # 2001-09-09T01:46:40Z
        s.utime("times.cfg", (stamp, stamp))
        st = os.stat(self.real("times.cfg"))
        self.assertEqual(int(st.st_mtime), stamp,
                         "utime が成功と答えたのに更新日時が変わっていない")
        self.assertEqual(int(st.st_atime), stamp)

    def test_chmod_still_works(self):
        """これまで効いていた chmod は、そのまま効くこと。"""
        self._seed("mode.cfg")
        s = self.sftp()
        s.chmod("mode.cfg", 0o444)
        self.addCleanup(os.chmod, self.real("mode.cfg"), 0o666)
        self.assertEqual(os.stat(self.real("mode.cfg")).st_mode & 0o444,
                         0o444)

    def test_a_failing_setstat_is_reported(self):
        """存在しないファイルへの要求は、成功と答えないこと。"""
        s = self.sftp()
        with self.assertRaises(IOError):
            s.truncate("no-such-file.cfg", 0)


if __name__ == "__main__":
    unittest.main()
