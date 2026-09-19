"""置き換えのアップロードで、一時名が作られた瞬間から既存と同じ mode であることを検証する。

1 周目（sftpc-02）で、置き換えの一時名は put() の前に空で作り、既存の mode を
ハンドルへの chmod（FSETSTAT）で当ててから中身を送るようにした。ただし作る
ときの OPEN は paramiko の open() が属性なしで送るので、OPEN から FSETSTAT
までの 1 往復のあいだ、一時名はサーバの既定の mode になる。

実測（下のサーバ、umask 022）: 既存 0600 の startup.cfg を上書きで置き換えると、
一時名は OPEN の時点で 0644 で作られた（created =
[('/home/admin/.startup.cfg.netbelt-part.31632-cbe060cd', 420, False)]、
最後の False は OPEN に permissions 属性が無かったこと）。中身の WRITE は
0600 になってから届くが、権限の確認は open(2) の時点でしか行われないので、
その瞬間に他のユーザーが開いた読み取りハンドルは chmod のあとも有効なまま
残り、続けて書かれる設定の中身を読める。

直し方: 一時名を作る OPEN に permissions 属性（既存の mode）を付ける。paramiko
の公開 API では付けられないので、open() と同じ要求（WRITE|CREATE|EXCL）を
SFTPAttributes 付きで _request から送り、SFTPFile に包む。OpenSSH の
sftp-server は a.perm を open(2) の mode に渡す。umask で落ちたビットは、
これまでどおりハンドルへの chmod で当て直す。mode 付きの OPEN を断る機器では、
これまでどおり属性なしで開く。本物の OpenSSH の sftp-server（Git for Windows
同梱）でも、この OPEN が受け付けられて置き換えまで終わることを確かめた
（その環境のファイルシステムは mode を反映しないので、mode はこのサーバで見る）。

サーバは localhost の paramiko で立てる。OpenSSH の sftp-server と同じく、
OPEN に permissions 属性があればそれを、無ければ 0666 を作成時の mode とし、
umask（022）を引いて作る。既存を O_TRUNC で開いても mode は変えない。
"""
import os
import posixpath
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import paramiko
from paramiko import (SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface,
                      SFTP_FAILURE, SFTP_NO_SUCH_FILE, SFTP_OK, SFTP_OP_UNSUPPORTED)

sys.path.insert(0, "src")

USER = "admin"
PASSWORD = "test-password"
HOME = "/home/admin"
UMASK = 0o022
FINAL = HOME + "/startup.cfg"


class _Node:
    def __init__(self, mode, data=b""):
        self.mode = mode
        self.data = bytearray(data)


class _MemoryFS:
    """OpenSSH の sftp-server と同じ mode の規則で振る舞うメモリ上のファイル群"""

    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}
        # OPEN で作ったファイル: (パス, 作った時点の mode, OPEN に permissions があったか)
        self.created = []
        self.writes = []                 # (パス, その WRITE の時点の mode)
        self.refuse_open_attrs = False   # True なら permissions 付きの OPEN を断る

    def norm(self, path):
        if not path.startswith("/"):
            path = posixpath.join(HOME, path)
        return posixpath.normpath(path)

    def attrs(self, path):
        node = self.files[path]
        a = SFTPAttributes()
        a.st_mode = stat.S_IFREG | node.mode
        a.st_size = len(node.data)
        a.st_uid = a.st_gid = 1000
        a.st_atime = a.st_mtime = 0
        a.filename = posixpath.basename(path)
        return a

    def created_parts(self):
        """一時名（.netbelt-part）を作った OPEN の記録"""
        return [c for c in self.created if ".netbelt-part" in c[0]]

    def parts(self):
        return [p for p in self.files if ".netbelt-part" in p]


class _Handle(SFTPHandle):
    def __init__(self, fs, path, flags):
        super().__init__(flags)
        self.fs = fs
        self.path = path

    def write(self, offset, data):
        with self.fs.lock:
            node = self.fs.files.get(self.path)
            if node is None:
                return SFTP_FAILURE
            self.fs.writes.append((self.path, node.mode))
            if len(node.data) < offset:
                node.data.extend(b"\0" * (offset - len(node.data)))
            node.data[offset:offset + len(data)] = data
        return SFTP_OK

    def stat(self):
        with self.fs.lock:
            return self.fs.attrs(self.path)

    def chattr(self, attr):
        with self.fs.lock:
            if attr._flags & attr.FLAG_PERMISSIONS:
                self.fs.files[self.path].mode = attr.st_mode & 0o7777
        return SFTP_OK


class _SftpInterface(SFTPServerInterface):
    def __init__(self, server, fs):
        super().__init__(server)
        self.fs = fs

    def canonicalize(self, path):
        return self.fs.norm(path)

    def list_folder(self, path):
        with self.fs.lock:
            d = self.fs.norm(path)
            return [self.fs.attrs(p) for p in self.fs.files
                    if posixpath.dirname(p) == d]

    def stat(self, path):
        with self.fs.lock:
            p = self.fs.norm(path)
            if p not in self.fs.files:
                return SFTP_NO_SUCH_FILE
            return self.fs.attrs(p)

    lstat = stat

    def open(self, path, flags, attr):
        with self.fs.lock:
            p = self.fs.norm(path)
            node = self.fs.files.get(p)
            has_perm = bool(attr._flags & attr.FLAG_PERMISSIONS)
            if has_perm and self.fs.refuse_open_attrs:
                return SFTP_OP_UNSUPPORTED
            if node is not None and flags & os.O_EXCL:
                return SFTP_FAILURE
            if node is None:
                if not flags & os.O_CREAT:
                    return SFTP_NO_SUCH_FILE
                # sftp-server: mode = a.perm（無ければ 0666）を open(2) へ渡す
                requested = attr.st_mode & 0o7777 if has_perm else 0o666
                node = self.fs.files[p] = _Node(requested & ~UMASK)
                self.fs.created.append((p, node.mode, has_perm))
            elif flags & os.O_TRUNC:
                node.data = bytearray()     # open(2) と同じく mode は変えない
            return _Handle(self.fs, p, flags)

    def remove(self, path):
        with self.fs.lock:
            if self.fs.files.pop(self.fs.norm(path), None) is None:
                return SFTP_NO_SUCH_FILE
            return SFTP_OK

    def rename(self, oldpath, newpath):
        with self.fs.lock:
            o, n = self.fs.norm(oldpath), self.fs.norm(newpath)
            if o not in self.fs.files:
                return SFTP_NO_SUCH_FILE
            if n in self.fs.files:
                return SFTP_FAILURE
            self.fs.files[n] = self.fs.files.pop(o)
            return SFTP_OK

    def posix_rename(self, oldpath, newpath):
        with self.fs.lock:
            o, n = self.fs.norm(oldpath), self.fs.norm(newpath)
            if o not in self.fs.files:
                return SFTP_NO_SUCH_FILE
            self.fs.files[n] = self.fs.files.pop(o)
            return SFTP_OK

    def chattr(self, path, attr):
        with self.fs.lock:
            p = self.fs.norm(path)
            if p not in self.fs.files:
                return SFTP_NO_SUCH_FILE
            if attr._flags & attr.FLAG_PERMISSIONS:
                self.fs.files[p].mode = attr.st_mode & 0o7777
            return SFTP_OK


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
    """localhost の SSH サーバ。SFTP サブシステムで _MemoryFS を出す"""

    def __init__(self, fs):
        self.fs = fs
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
            t.set_subsystem_handler("sftp", SFTPServer, _SftpInterface, self.fs)
            t.start_server(server=_Auth())
            self.transports.append(t)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for t in list(self.transports):
            t.close()


class SftpUploadTmpCreatedModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-created-mode-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "startup.cfg")
        self.content = b"username admin secret 5 EXAMPLE-HASH\n" * 2000
        with open(self.local, "wb") as f:
            f.write(self.content)
        self.fs = _MemoryFS()
        self.server = _Server(self.fs)
        self.addCleanup(self.server.close)

    def _connect(self):
        from core.sftp_manager import SFTPManager

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", port=self.server.port, username=USER,
                    password=PASSWORD, look_for_keys=False, allow_agent=False,
                    timeout=10)
        self.addCleanup(ssh.close)
        m = SFTPManager()
        self.assertTrue(m.connect(ssh), "SFTP に接続できない")
        self.addCleanup(m.disconnect)
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        return m

    def _upload(self, m, remote=FINAL):
        m.upload_file(self.local, remote, overwrite=True)
        deadline = time.time() + 15
        while time.time() < deadline and not (self.done or self.errors):
            self.app.processEvents()
            time.sleep(0.02)
        self.assertTrue(self.done or self.errors, "終わらない")
        self.assertEqual(self.errors, [])

    def _write_modes(self):
        return sorted({oct(mode) for path, mode in self.fs.writes
                       if ".netbelt-part" in path})

    def _assert_final(self, remote, mode):
        final = self.fs.files[remote]
        self.assertEqual(oct(final.mode), oct(mode))
        self.assertEqual(bytes(final.data), self.content)
        self.assertEqual(self.fs.parts(), [], "一時名が残っている")

    def test_the_temporary_name_is_created_with_the_existing_mode(self):
        """一時名を作った OPEN の時点で、既存と同じ 0600 であること。"""
        self.fs.files[FINAL] = _Node(0o600, b"old secret\n")
        m = self._connect()

        self._upload(m)

        created = self.fs.created_parts()
        self.assertEqual(len(created), 1, "一時名の作成が記録されていない: %s" % created)
        self.assertEqual(
            oct(created[0][1]), "0o600",
            "一時名がサーバの既定の mode で作られ、chmod までのあいだ既存（0600）"
            "より緩い: %s" % (created,))
        self.assertEqual(self._write_modes(), ["0o600"])
        self._assert_final(FINAL, 0o600)

    def test_bits_dropped_by_the_umask_are_put_back_before_the_first_write(self):
        """umask で落ちたビット（0664 の g+w）は、中身を書く前に当て直すこと。"""
        self.fs.files[FINAL] = _Node(0o664, b"old\n")
        m = self._connect()

        self._upload(m)

        self.assertEqual(self._write_modes(), ["0o664"])
        self._assert_final(FINAL, 0o664)

    def test_a_server_that_refuses_a_mode_in_open_still_completes(self):
        """permissions 付きの OPEN を断る機器でも、これまでどおり送れること。"""
        self.fs.files[FINAL] = _Node(0o600, b"old secret\n")
        self.fs.refuse_open_attrs = True
        m = self._connect()

        self._upload(m)

        # 断られたあと、属性なしの OPEN で作り直している
        self.assertEqual([perm for _, _, perm in self.fs.created_parts()], [False])
        self.assertEqual(self._write_modes(), ["0o600"])
        self._assert_final(FINAL, 0o600)

    def test_a_new_name_is_still_created_with_the_server_default(self):
        """既存が無ければ、これまでどおりサーバの既定の mode で作ること。"""
        m = self._connect()

        self._upload(m, HOME + "/new.cfg")

        self.assertEqual([(mode, perm) for _, mode, perm in self.fs.created_parts()],
                         [(0o666 & ~UMASK, False)])
        self._assert_final(HOME + "/new.cfg", 0o666 & ~UMASK)


if __name__ == "__main__":
    unittest.main()
