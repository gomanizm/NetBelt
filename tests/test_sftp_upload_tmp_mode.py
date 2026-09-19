"""置き換えのアップロード中、一時名が既存ファイルより緩い権限にならないことを検証する。

upload_file は一時名へ送ってから最終名へ改名する（v1.3.0 から）。既存の
mode は改名の前に一時名へ当て直しているが、それは全データを送ったあとで、
転送中の保護は無かった。

実測（OpenSSH の sftp-server と同じ規則のサーバ）: 既存の startup.cfg (0600) を
上書きで置き換えると、put() は OPEN に mode を付けないので、一時名は
サーバの既定 0666 & ~022 = 0644 で作られた。WRITE のたびに一時名の mode を
見ると 7 回とも 0644 で、0600 への chmod は全データを送ったあとの 1 回だけ
だった。転送途中で接続が切れると、途中までの設定内容を持つ一時名が 0644 の
まま機器に残った。親ディレクトリを他人が読めるサーバでは、そのあいだ
ほかのローカルユーザーが中身を読める。一時名で送る作りより前は既存ファイルを
O_TRUNC で開いていたので、mode は保たれていた。

直し方: 置き換えのときは put() の前に一時名を空で作り、中身を書く前に既存の
mode を当ててから閉じる。put() は O_TRUNC で開き直すだけなので mode は
そのまま残る。既存の mode が読めない（新しい名前）ときは、これまでどおり
サーバの既定で作る。

サーバは localhost の paramiko で立てる。OpenSSH の sftp-server と同じく、
OPEN に mode が無ければ 0666 & ~umask で作り、既存を O_TRUNC で開いても
mode は変えない、メモリ上のファイル群を出す。
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


class _Node:
    def __init__(self, mode, data=b""):
        self.mode = mode
        self.data = bytearray(data)


class _MemoryFS:
    """OpenSSH の sftp-server と同じ mode の規則で振る舞うメモリ上のファイル群"""

    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}
        self.writes = []            # (パス, オフセット, その WRITE の時点の mode)
        self.on_write = None        # WRITE のあとに呼ぶ（切断の再現に使う）
        self.handle_chattr = True   # False ならハンドルへの chmod（fsetstat）を断る

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

    def parts(self):
        """一時名（.netbelt-part）として残っているパスの一覧"""
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
            self.fs.writes.append((self.path, offset, node.mode))
            if len(node.data) < offset:
                node.data.extend(b"\0" * (offset - len(node.data)))
            node.data[offset:offset + len(data)] = data
        if self.fs.on_write is not None and self.fs.on_write(self.path, offset):
            return SFTP_FAILURE
        return SFTP_OK

    def stat(self):
        with self.fs.lock:
            return self.fs.attrs(self.path)

    def chattr(self, attr):
        if not self.fs.handle_chattr:
            return SFTP_OP_UNSUPPORTED
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
            if node is None:
                if not flags & os.O_CREAT:
                    return SFTP_NO_SUCH_FILE
                requested = (attr.st_mode & 0o7777
                             if attr._flags & attr.FLAG_PERMISSIONS else 0o666)
                self.fs.files[p] = _Node(requested & ~UMASK)
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

    def drop(self):
        """機器側から接続を切る（再起動・回線断の再現）"""
        for t in list(self.transports):
            t.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.drop()


class SftpUploadTmpModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-tmpmode-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "startup.cfg")
        # 複数回の WRITE に分かれる大きさにする（paramiko は 32KiB ずつ送る）
        self.content = b"username admin secret 5 EXAMPLE-HASH\n" * 6000
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

    def _wait(self, predicate, seconds=15.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _existing(self, mode=0o600):
        self.fs.files[HOME + "/startup.cfg"] = _Node(mode, b"old secret\n")

    def _modes_at_write(self):
        return [(offset, oct(mode)) for path, offset, mode in self.fs.writes
                if ".netbelt-part" in path]

    def test_the_temporary_name_keeps_the_existing_mode_from_the_first_write(self):
        """最初の WRITE の時点から、一時名が既存と同じ 0600 であること。"""
        self._existing(0o600)
        m = self._connect()

        m.upload_file(self.local, HOME + "/startup.cfg", overwrite=True)

        self.assertTrue(self._wait(lambda: self.done or self.errors), "終わらない")
        self.assertEqual(self.errors, [])
        modes = self._modes_at_write()
        self.assertGreater(len(modes), 1, "一時名への WRITE が記録されていない")
        self.assertEqual(
            [mode for _, mode in modes], ["0o600"] * len(modes),
            "転送中の一時名が既存（0600）より緩い: %s" % modes)
        final = self.fs.files[HOME + "/startup.cfg"]
        self.assertEqual(oct(final.mode), "0o600")
        self.assertEqual(bytes(final.data), self.content)
        self.assertEqual(self.fs.parts(), [], "一時名が残っている")

    def test_a_temporary_name_left_by_a_dropped_connection_keeps_the_existing_mode(self):
        """転送途中で切れて機器に残った一時名も、既存と同じ 0600 であること。"""
        self._existing(0o600)
        m = self._connect()

        def drop_after_the_first_write(path, offset):
            if ".netbelt-part" in path:
                self.server.drop()
                return True
            return False

        self.fs.on_write = drop_after_the_first_write
        m.upload_file(self.local, HOME + "/startup.cfg", overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors), "切断が通知されない")
        self.assertEqual(self.done, [])
        left = self.fs.parts()
        self.assertEqual(len(left), 1, "途中までの一時名が残っていない: %s" % left)
        self.assertEqual(
            oct(self.fs.files[left[0]].mode), "0o600",
            "途中までの設定内容が既存（0600）より緩い権限で残っている")
        # 最終名には触っていない
        final = self.fs.files[HOME + "/startup.cfg"]
        self.assertEqual((oct(final.mode), bytes(final.data)),
                         ("0o600", b"old secret\n"))

    def test_a_server_that_refuses_a_handle_chmod_still_completes(self):
        """ハンドルへの chmod を断る機器でも、転送は成功し最終名は既存の mode になること。"""
        self._existing(0o600)
        self.fs.handle_chattr = False
        m = self._connect()

        m.upload_file(self.local, HOME + "/startup.cfg", overwrite=True)

        self.assertTrue(self._wait(lambda: self.done or self.errors), "終わらない")
        self.assertEqual(self.errors, [])
        final = self.fs.files[HOME + "/startup.cfg"]
        self.assertEqual(oct(final.mode), "0o600")
        self.assertEqual(bytes(final.data), self.content)

    def test_a_new_name_keeps_the_server_default_mode(self):
        """既存が無ければ、これまでどおりサーバの既定の mode で作ること。"""
        m = self._connect()

        m.upload_file(self.local, HOME + "/new.cfg", overwrite=True)

        self.assertTrue(self._wait(lambda: self.done or self.errors), "終わらない")
        self.assertEqual(self.errors, [])
        final = self.fs.files[HOME + "/new.cfg"]
        self.assertEqual(oct(final.mode), oct(0o666 & ~UMASK))
        self.assertEqual(bytes(final.data), self.content)


if __name__ == "__main__":
    unittest.main()
