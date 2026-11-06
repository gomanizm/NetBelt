# -*- coding: utf-8 -*-
"""UTF-8 として読めない名前が 1 件あっても、ディレクトリの一覧が出ることを検証する。

paramiko の listdir_attr は READDIR の名前を errors 指定なしの UTF-8 で読むので、
cp932 などの名前（Windows で作った tar を Linux で展開した、など）が 1 件でも
あると UnicodeDecodeError で抜ける。これまでは同じディレクトリの普通の
ファイルも 1 件も表示されず、生の codec エラーだけが出ていた。しかも
listdir_attr の末尾の CMD_CLOSE が送られないので、更新するたびに機器側に
ディレクトリのハンドルが 1 つずつ残っていた。

読めない名前の項目だけ置き換え文字で表示して一覧を続け、ハンドルは閉じること。
"""
import os
import posixpath
import socket
import sys
import threading
import time
import unittest

import paramiko
from paramiko import (SFTPAttributes, SFTPServer, SFTPServerInterface,
                      SFTP_NO_SUCH_FILE)

sys.path.insert(0, "src")

USER = "admin"
PASSWORD = "test-password"
HOME = "/home/admin"
# 「設定.txt」を cp932 で符号化したもの（UTF-8 としては読めない）
BAD_NAME = "設定.txt".encode("cp932")


class _SftpInterface(SFTPServerInterface):
    """HOME に config.txt と、名前が UTF-8 でない 1 件を置く"""


    def canonicalize(self, path):
        return posixpath.normpath(posixpath.join(HOME, path))

    def list_folder(self, path):
        if posixpath.normpath(path) != HOME:
            return SFTP_NO_SUCH_FILE
        good = SFTPAttributes()
        good.filename = "config.txt"
        good.st_mode = 0o100644
        good.st_size = 12
        good.st_mtime = 1700000000
        bad = SFTPAttributes()
        # paramiko のサーバーは bytes の名前をそのまま送る
        bad.filename = BAD_NAME
        bad.st_mode = 0o100644
        bad.st_size = 1
        bad.st_mtime = 1700000000
        return [good, bad]

    def stat(self, path):
        if posixpath.normpath(path) == HOME:
            attr = SFTPAttributes()
            attr.st_mode = 0o040755
            return attr
        return SFTP_NO_SUCH_FILE

    lstat = stat


class _RecordingSFTPServer(SFTPServer):
    """機器側のディレクトリハンドル（folder_table）を見るために控える"""

    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).instances.append(self)


class _Auth(paramiko.ServerInterface):
    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        if (username, password) == (USER, PASSWORD):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _Server:
    """localhost の SSH サーバ。SFTP サブシステムで上の一覧を出す"""

    def __init__(self):
        self.key = paramiko.ECDSAKey.generate()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.key)
            t.set_subsystem_handler("sftp", _RecordingSFTPServer, _SftpInterface)
            t.start_server(server=_Auth())
            self.transports.append(t)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for t in list(self.transports):
            t.close()


class SftpNonUtf8ListingTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        _RecordingSFTPServer.instances = []
        self.server = _Server()
        self.addCleanup(self.server.close)

    def _pump(self, check, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _connect(self):
        from core.sftp_manager import SFTPManager

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", port=self.server.port, username=USER,
                    password=PASSWORD, look_for_keys=False, allow_agent=False,
                    timeout=10)
        self.addCleanup(ssh.close)
        m = SFTPManager()
        type(self)._keep.append(m)
        self.assertTrue(m.connect(ssh), "SFTP に接続できない")
        self.addCleanup(m.disconnect)
        self.errors, self.lists = [], []
        m.error_occurred.connect(self.errors.append)
        m.file_list_ready.connect(self.lists.append)
        return m

    def _list(self, m):
        self.errors.clear()
        self.lists.clear()
        m.list_directory(HOME)
        self.assertTrue(self._pump(lambda: self.errors or self.lists),
                        "一覧の結果が届かない")

    def test_the_other_entries_are_still_listed(self):
        """読めない名前が 1 件あっても、ほかの項目は一覧に出ること。"""
        m = self._connect()
        self._list(m)

        self.assertEqual(self.errors, [], "一覧が失敗した")
        names = [e["name"] for e in self.lists[0]]
        self.assertIn("config.txt", names)
        self.assertEqual(len(names), 2, names)
        self.assertTrue(m.is_connected)

    def test_the_unreadable_name_is_shown_with_replacement_characters(self):
        """読めない名前は置き換え文字を含む名前として並ぶこと。"""
        m = self._connect()
        self._list(m)

        self.assertEqual(self.errors, [])
        others = [e["name"] for e in self.lists[0] if e["name"] != "config.txt"]
        self.assertEqual(len(others), 1, others)
        self.assertEqual(others[0],
                         BAD_NAME.decode("utf-8", errors="replace"))

    def test_the_directory_handle_is_closed_after_each_listing(self):
        """一覧のたびに機器側のディレクトリハンドルを閉じること。"""
        m = self._connect()
        for _ in range(3):
            self._list(m)
            self.assertEqual(self.errors, [])
        servers = _RecordingSFTPServer.instances
        self.assertEqual(len(servers), 1)
        self.assertTrue(
            self._pump(lambda: len(servers[0].folder_table) == 0, 2),
            "機器側にディレクトリのハンドルが残っている: %d 個"
            % len(servers[0].folder_table))


if __name__ == "__main__":
    unittest.main()
