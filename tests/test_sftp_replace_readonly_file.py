"""書込みビットの無い既存ファイルを、これまでどおり置き換えられることを検証する。

何が起きていたか（実測）:
  1 周目（sftpc-02）で、置き換えの一時名は put() の前に空で作り、既存と同じ
  mode を当てるようにした（_create_tmp_with_mode）。ところが作ったハンドルは
  そこで閉じ、続く put() が同じ名前を 'wb' で開き直す。既存が 0444 や 0400 の
  ように所有者の書込みビットを持たないと、この開き直しが open(2) の権限検査で
  断られる。

  下のメモリ FS サーバ（OpenSSH と同じ検査を足したもの）で /home/admin/startup.cfg
  を mode=0o444 にして overwrite=True で置き換えると:
    done  : []  /  errors: ['アップロードエラー: [Errno 13] Permission denied']
    OPEN の記録: ('… .netbelt-part.…', 1281, 'created mode=0o444')
                ('… .netbelt-part.…', 769, 'PERMISSION_DENIED mode=0o444')
    最終名の中身が新しいか: False
  mode=0o400 でも同じで、0o644 なら成功する。_create_tmp_with_mode を no-op に
  すると 0o444 でも成功したので、前周の修正が入れた退行である。利用者に出るのは
  「[Errno 13] Permission denied」だけで、親ディレクトリでも最終名でもなく
  一時名の mode が原因だとは分からない。

どう直したか:
  一時名を作るときの mode に所有者の書込みビットを足す（(mode & 0o7777) | 0o200）。
  他のユーザーへの見え方は広がらず（0444→0644 は元から o+r、0400→0600）、
  転送後の _carry_over_mode が最終的に元どおりの mode を当て直す。OPEN へ渡す
  permissions と、その直後のハンドルへの chmod の両方を同じ値にする。
  代替案（書込みビットの無い既存では一時名の事前作成を省く）は、0400 の
  ファイルで一時名がサーバ既定（0644 等）になり、前周に直した情報漏れが戻る。

サーバは localhost の paramiko で立てる。test_sftp_upload_tmp_created_mode.py の
メモリ FS に、OpenSSH と同じ open(2) の権限検査（所有者・非特権で書込みビットの
無い既存を書込みで開くと SFTP_PERMISSION_DENIED）を足したもの。
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
                      SFTP_FAILURE, SFTP_NO_SUCH_FILE, SFTP_OK, SFTP_OP_UNSUPPORTED,
                      SFTP_PERMISSION_DENIED)

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
    """OpenSSH の sftp-server と同じ mode / 権限の規則で振る舞うファイル群"""

    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}
        self.created = []                # (パス, 作った時点の mode, permissions 付きか)
        self.writes = []                 # (パス, その WRITE の時点の mode)
        self.denied = []                 # 権限で断った OPEN の (パス, mode)
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
                return _Handle(self.fs, p, flags)
            # 既存を開く。open(2) はこの瞬間の mode で権限を見る（所有者・非特権）
            if flags & (os.O_WRONLY | os.O_RDWR) and not node.mode & 0o200:
                self.fs.denied.append((p, node.mode))
                return SFTP_PERMISSION_DENIED
            if flags & os.O_TRUNC:
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


class SftpReplaceReadonlyFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-readonly-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "startup.cfg")
        self.content = b"hostname router-example\n" * 500
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

    def _replace(self, mode):
        """既存を mode で置いてから、承認済みの置き換えを送る"""
        self.fs.files[FINAL] = _Node(mode, b"old config\n")
        m = self._connect()
        m.upload_file(self.local, FINAL, overwrite=True)
        deadline = time.time() + 20
        while time.time() < deadline and not (self.done or self.errors):
            self.app.processEvents()
            time.sleep(0.02)
        self.assertTrue(self.done or self.errors, "終わらない")
        return m

    def _assert_replaced(self, mode):
        self.assertEqual(
            self.errors, [],
            "書込みビットの無い既存（%s）を置き換えられない: %s" % (oct(mode), self.errors))
        self.assertEqual(self.done, ["アップロード完了: startup.cfg"])
        final = self.fs.files[FINAL]
        self.assertEqual(bytes(final.data), self.content, "最終名の中身が古いまま")
        self.assertEqual(oct(final.mode), oct(mode), "元の mode に戻していない")
        self.assertEqual(self.fs.parts(), [], "一時名が残っている")
        self.assertEqual(self.fs.denied, [],
                         "権限で断られた OPEN がある: %s" % (self.fs.denied,))

    def test_a_read_only_file_can_still_be_replaced(self):
        """0444 の既存を、承認済みの置き換えで差し替えられること。"""
        self._replace(0o444)

        self._assert_replaced(0o444)

    def test_an_owner_only_read_file_can_still_be_replaced(self):
        """0400 の既存でも同じであること。"""
        self._replace(0o400)

        self._assert_replaced(0o400)

    def test_the_temporary_name_only_gains_the_owner_write_bit(self):
        """一時名に足すのは所有者の書込みビットだけで、他人への見え方は広げないこと。"""
        self._replace(0o400)

        created = self.fs.created_parts()
        self.assertEqual(len(created), 1, "一時名の作成が記録されていない: %s" % created)
        self.assertEqual(oct(created[0][1]), oct(0o600),
                         "一時名の mode が 0400|0200 になっていない: %s" % (created,))
        during = sorted({mode for path, mode in self.fs.writes
                         if ".netbelt-part" in path})
        self.assertEqual([oct(mode) for mode in during], [oct(0o600)],
                         "中身を書いている間の mode が想定と違う: %s" % (during,))
        self.assertEqual([mode & 0o077 for mode in during], [0o400 & 0o077],
                         "他のユーザーへの見え方が広がっている: %s" % (during,))

    def test_an_already_writable_file_is_unchanged(self):
        """所有者が書ける既存（0644）は、これまでどおりそのままであること。"""
        self._replace(0o644)

        self._assert_replaced(0o644)
        self.assertEqual([oct(mode) for _, mode, _ in self.fs.created_parts()],
                         [oct(0o644)], "書込みビットのある mode を変えている")

    def test_a_server_that_refuses_a_mode_in_open_still_replaces(self):
        """permissions 付きの OPEN を断る機器でも、0444 を置き換えられること。"""
        self.fs.refuse_open_attrs = True

        self._replace(0o444)

        self._assert_replaced(0o444)
        # 断られたあと、属性なしの OPEN で作り直している
        self.assertEqual([perm for _, _, perm in self.fs.created_parts()], [False])


if __name__ == "__main__":
    unittest.main()
