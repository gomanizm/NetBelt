"""日本語のホスト名の行と「|1|」が同じ known_hosts にあっても、その機器の鍵を照合に使うことを検証する。

何が起きていたか（b3c3eed で実測。441ea02 では起きない）。ハッシュ化名の行
（|1|salt|hash）があるファイルを、読み込みの 2 乗の重さを避けるため自前の
ローダ（load_known_hosts）で読むようにした。自前のローダは UTF-8 で読み、
読めないバイトは置き換える。一方 NetBelt 自身は HostKeys.save（text モード、
既定の文字コード）で書くので、日本語の Windows（cp932）では日本語の
ホスト名が cp932 のまま保存される。

    東京-sw ecdsa-sha2-nistp256 AAAA...     （ディスク上は 93 8C 8B 9E 2D 73 77 ...）

この行があるファイルに、OpenSSH から写したハッシュ化行（別の機器）や
「|1|」を含むコメント行が 1 行でも入ると、自前のローダが名前を文字化けさせ、
東京-sw は「未知」に戻る。鍵 B の相手へ繋ぐと TOFU が受け入れ、パスワードが
相手へ届いていた（auth=[('admin', 's3cret')]。そのあと保存も
『'cp932' codec can't encode character』で失敗する）。441ea02 は paramiko に
cp932 で読ませていたので『ホストキーが変更されています』で止まっていた。

どう直したか。ハッシュ化名があるときに自前のローダへ回すのは、ファイルが
ASCII だけのときに限る。ASCII 以外を含むファイルは今までどおり paramiko に
読ませる（読み込みの重さも今までどおり）。

テストの前提: paramiko.hostkeys の open を、文字コードを指定しないテキストを
cp932 で開くものに差し替える（日本語の Windows の既定と同じ）。どの環境で
流しても、NetBelt が日本語の Windows で書いた known_hosts と同じ読み書きになる。
"""
import builtins
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402
from paramiko.hostkeys import HostKeys              # noqa: E402

HOST = "東京-sw"


def _open_with_cp932_default(file, mode="r", buffering=-1, encoding=None,
                             *args, **kwargs):
    """文字コードを指定しないテキストの open を、日本語の Windows と同じ cp932 で開く"""
    if "b" not in mode and encoding is None:
        encoding = "cp932"
    return builtins.open(file, mode, buffering, encoding, *args, **kwargs)


class _RecordingServer(paramiko.ServerInterface):
    """届いた認証を記録し、必ず拒む。"""

    def __init__(self, log):
        self.log = log

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        self.log.append((username, password))
        return paramiko.AUTH_FAILED


class _LocalSSHServer:
    """localhost のエフェメラルポートで待ち受ける paramiko サーバ。"""

    def __init__(self, key):
        self.key = key
        self.auth_log = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                accepted, _ = self.sock.accept()
            except OSError:
                return
            transport = paramiko.Transport(accepted)
            transport.add_server_key(self.key)
            self.transports.append(transport)
            try:
                transport.start_server(server=_RecordingServer(self.auth_log))
            except Exception:
                pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for transport in self.transports:
            transport.close()


class KnownHostsHashedNonAsciiNameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()
        cls.key_c = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khjp-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        for patcher in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=self.dir),
                mock.patch.object(paramiko.hostkeys, "open",
                                  _open_with_cp932_default, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _line(self, name, key):
        return "%s %s %s\n" % (name, key.get_name(), key.get_base64())

    def _connect(self, presented_key):
        """presented_key の相手へ HOST で繋ぐ（名前解決は localhost へ向ける）。"""
        server = _LocalSSHServer(presented_key)
        self.addCleanup(server.close)
        real_port = server.port

        def to_local_server(client, hostname, port):
            return [(socket.AF_INET, ("127.0.0.1", real_port))]

        from core.ssh_connection import SSHConnection
        conn = SSHConnection(HOST, 22, "admin", password="s3cret")
        self.addCleanup(conn.deleteLater)
        errors, outputs = [], []
        conn.error_occurred.connect(errors.append)
        conn.output_received.connect(outputs.append)
        with mock.patch.object(paramiko.SSHClient, "_families_and_addresses",
                               to_local_server):
            returned = conn.connect()
        conn.dispose()
        return returned, errors, list(server.auth_log), outputs

    def _layouts(self):
        """NetBelt が cp932 で書いた行と、「|1|」を含む行の組み合わせ。"""
        mine = self._line(HOST, self.key_a).encode("cp932")
        hashed = self._line(HostKeys.hash_host("198.51.100.9"),
                            self.key_c).encode("ascii")
        comment = b"# copied |1| from openssh\n"
        return {
            "cp932 line + hashed line": mine + hashed,
            "hashed line + cp932 line": hashed + mine,
            "cp932 line + comment with |1|": mine + comment,
        }

    def test_cp932_name_is_still_verified_with_hashed_names(self):
        """保存済みの鍵と違う相手（鍵 B）へ認証を送らず、ファイルも書き換えないこと。"""
        for title, data in self._layouts().items():
            with self.subTest(title):
                self.known_hosts.write_bytes(data)
                # 前提: UTF-8 では読めないバイト列になっていること
                with self.assertRaises(UnicodeDecodeError):
                    data.decode("utf-8")

                returned, errors, auth, _ = self._connect(self.key_b)

                self.assertFalse(returned)
                self.assertEqual(auth, [],
                                 "保存済みの鍵と違う相手へ認証が届いている")
                self.assertTrue(
                    any("ホストキーが変更されています" in e for e in errors),
                    errors)
                self.assertEqual(self.known_hosts.read_bytes(), data)

    def test_cp932_name_accepts_the_saved_key(self):
        """保存済みの鍵 A の相手には、既知の機器として認証へ進むこと。

        「未知」に戻っていると、TOFU が保存を試みて警告
        （known_hosts を保存できません）を出す。
        """
        for title, data in self._layouts().items():
            with self.subTest(title):
                self.known_hosts.write_bytes(data)

                returned, errors, auth, outputs = self._connect(self.key_a)

                self.assertEqual(auth, [("admin", "s3cret")], errors)
                self.assertEqual([o for o in outputs if "警告" in o], [])
                self.assertEqual(self.known_hosts.read_bytes(), data)

    def test_ascii_file_with_hashed_names_uses_own_loader(self):
        """ASCII だけのファイルは、ハッシュ化名があれば今までどおり自前で読むこと。"""
        from core import ssh_connection
        self.known_hosts.write_bytes(
            (self._line(HostKeys.hash_host("198.51.100.9"), self.key_c)
             + self._line("198.51.100.10", self.key_a)).encode("ascii"))
        self.assertTrue(
            ssh_connection._hashed_names_need_own_loader(self.known_hosts))


if __name__ == "__main__":
    unittest.main()
