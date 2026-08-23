"""ルートディレクトリ配下のリンク経由で外へ出られないことを確認する。

`os.path.abspath()` は `..` を畳むだけでシンボリックリンクやジャンクションを
解決しない。root 配下にリンクを1つ置ければ、その先の任意の場所を読み書きできる。

Windows ではシンボリックリンクの作成に権限が要るが、**ディレクトリジャンクションは
一般ユーザーでも作成できる**ため、実際に到達しうる経路である。
"""
import os
import socket
import subprocess
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


def make_link(link_path, target_dir):
    """ディレクトリへのリンクを作る。作れない環境では None を返す。"""
    if sys.platform == "win32":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link_path, target_dir],
                           capture_output=True)
        return link_path if r.returncode == 0 else None
    try:
        os.symlink(target_dir, link_path)
        return link_path
    except OSError:
        return None


def remove_link(link_path):
    """リンク自体だけを消す（リンク先の中身は消さない）。"""
    try:
        if os.path.isdir(link_path):
            os.rmdir(link_path)      # ジャンクション/シンボリックリンクはこれで外れる
        elif os.path.exists(link_path):
            os.remove(link_path)
    except OSError:
        pass


class _LinkFixture(unittest.TestCase):
    """root の中から外の秘密ファイルへ繋がるリンクを用意する。"""

    def setup_link(self, root):
        base = tempfile.mkdtemp(prefix="netbelt-link-")
        secret_dir = os.path.join(base, "outside")
        os.makedirs(secret_dir)
        secret = os.path.join(secret_dir, "passwords.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("CLASSIFIED")

        link = os.path.join(root, "escape")
        if make_link(link, secret_dir) is None:
            self.skipTest("この環境ではディレクトリリンクを作成できない")
        self.addCleanup(remove_link, link)
        return secret


class TftpLinkConfinementTest(_LinkFixture):
    def test_junction_escape_is_denied(self):
        from core.tftp_server import _safe_join
        root = tempfile.mkdtemp(prefix="netbelt-tftp-link-")
        self.setup_link(root)

        with self.assertRaises(ValueError,
                               msg="リンク経由で root の外へ出られた"):
            _safe_join(root, "escape/passwords.txt")

    def test_normal_path_still_allowed(self):
        from core.tftp_server import _safe_join
        root = tempfile.mkdtemp(prefix="netbelt-tftp-link-")
        got = _safe_join(root, "sub/normal.cfg")
        self.assertTrue(os.path.abspath(got).startswith(os.path.realpath(root)))


class SftpLinkConfinementTest(_LinkFixture):
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

        cls.root = tempfile.mkdtemp(prefix="netbelt-sftp-link-")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()

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

    def test_read_through_junction_is_denied(self):
        secret = self.setup_link(self.root)
        s = self.sftp()
        with self.assertRaises(IOError, msg="リンク経由で外部ファイルを読めた"):
            s.open("escape/passwords.txt", "r")
        # 念のため、内容が漏れていないこと
        with open(secret, encoding="utf-8") as f:
            self.assertEqual(f.read(), "CLASSIFIED")

    def test_write_through_junction_is_denied(self):
        self.setup_link(self.root)
        s = self.sftp()
        with self.assertRaises(IOError, msg="リンク経由で外部へ書き込めた"):
            with s.open("escape/planted.txt", "w") as fh:
                fh.write(b"owned")

    def test_listdir_through_junction_is_denied(self):
        self.setup_link(self.root)
        s = self.sftp()
        with self.assertRaises(IOError, msg="リンク経由で外部を一覧できた"):
            s.listdir("escape")

    def test_normal_file_still_works(self):
        """リンク対策で通常の読み書きを壊していないこと。"""
        s = self.sftp()
        with s.open("plain.cfg", "w") as fh:
            fh.write(b"hostname R1")
        with s.open("plain.cfg", "r") as fh:
            self.assertEqual(fh.read(), b"hostname R1")


if __name__ == "__main__":
    unittest.main()
