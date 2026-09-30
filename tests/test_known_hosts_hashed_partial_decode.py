"""ハッシュ化名のある known_hosts が途中から既定の文字コードで読めなくても、読めた行の鍵で照合することを検証する。

何が起きていたか（ebbe593 で実測。441ea02 では起きない）。known_hosts に
ハッシュ化名（|1|salt|hash）の行があり、ファイルが 8 KiB を超え、先頭側に
NetBelt が既定の文字コード（日本語の Windows では cp932）で保存した日本語名の
行、後ろ側に cp932 では読めないバイト（OpenSSH から写した UTF-8 のコメントや
IDN の行）がある形:

    1 行目      : 東京-sw <鍵 A>                （cp932）
    続けて      : 198.51.100.x の平文 79 行、ハッシュ化 5 行
    末尾        : # 東京ラボの機器              （UTF-8）
    合計 15,119 バイト（cp932 では 15,110 バイト目で読めない）

東京-sw へ鍵 B の相手で繋ぐと、441ea02 は『ホストキーが変更されています』で
止まったが、ebbe593 は認証を送った（auth=[('admin', 's3cret')]）。そのあと
『known_hosts を保存できません（'cp932' codec can't encode character …）』の
警告が出て、ファイルは変わらない。

原因。_load_known_hosts_into_client は、ハッシュ化名のあるファイルを
_HostKeysLoadedLinearly（paramiko の HostKeys.load の読み方）で読む。
text モードの読み込みは 8 KiB ずつデコードするので、読めないバイトより前の
かたまりの行（cp932 の東京-sw を含む）は、UnicodeDecodeError の前に読めている。
ところが例外のとき、その読めた分を client へ移さずに捨て、自前のローダ
（load_known_hosts。UTF-8 で読み、読めないバイトは置き換える）だけで読み直して
いた。自前のローダは cp932 の名前を文字化けさせるので、東京-sw は「未知」に
戻り、TOFU が鍵 B を受け入れた。441ea02 は paramiko の client.load_host_keys に
読ませていたので、読めた分が client に残っていた。

どう直したか。_HostKeysLoadedLinearly の読み込みを try/finally で包み、
例外のときも読めたところまでを client へ移してから、自前のローダで残りを
足す（441ea02 と同じ読み方）。

テストの前提（どの環境でも同じ読み方にする）:
  - paramiko.hostkeys の open を、文字コードを指定しないテキストを cp932 で、
    8 KiB ずつデコードして読むものに差し替える（日本語の Windows の既定と同じ）。
  - locale.getpreferredencoding も cp932 を返すようにする（日本語の Windows と
    同じ。NetBelt 側が既定の文字コードを見る場合もそろう）。
  - paramiko の読み込みが、東京-sw の行を読んだあとで UnicodeDecodeError に
    なることを、テストの中で確かめる。
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
CHUNK = 8192


def _open_like_japanese_windows(file, mode="r", buffering=-1, encoding=None,
                                *args, **kwargs):
    """文字コードを指定しないテキストの open を cp932 で開き、8 KiB ずつデコードする"""
    text = "b" not in mode and encoding is None
    if text:
        encoding = "cp932"
    f = builtins.open(file, mode, buffering, encoding, *args, **kwargs)
    if text:
        f._CHUNK_SIZE = CHUNK
    return f


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


class KnownHostsHashedPartialDecodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()
        cls.key_c = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khpd-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        for patcher in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=self.dir),
                mock.patch.object(paramiko.hostkeys, "open",
                                  _open_like_japanese_windows, create=True),
                mock.patch("locale.getpreferredencoding",
                           return_value="cp932")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _line(self, name, key):
        return "%s %s %s\n" % (name, key.get_name(), key.get_base64())

    def _layouts(self):
        """先頭に cp932 の東京-sw、8 KiB を超える平文とハッシュ化行、末尾に UTF-8。"""
        mine = self._line(HOST, self.key_a).encode("cp932")
        filler = "".join(self._line("198.51.100.%d" % i, self.key_c)
                         for i in range(1, 80)).encode("ascii")
        hashed = "".join(self._line(HostKeys.hash_host("203.0.113.%d" % i),
                                    self.key_c)
                         for i in range(1, 6)).encode("ascii")
        return {
            "UTF-8 comment at the end":
                mine + filler + hashed + "# 東京ラボの機器\n".encode("utf-8"),
            "UTF-8 host name line at the end":
                mine + filler + hashed
                + self._line("東京ラボ-sw", self.key_c).encode("utf-8"),
        }

    def _write(self, data):
        """known_hosts を置き、paramiko が東京-sw を読んでから読めずに落ちることを確かめる。"""
        self.known_hosts.write_bytes(data)
        with self.assertRaises(UnicodeDecodeError) as caught:
            data.decode("cp932")
        # 読めないバイトは、先頭の 8 KiB より後ろにある
        self.assertGreater(caught.exception.start, CHUNK)
        partial = HostKeys()
        with self.assertRaises(UnicodeDecodeError):
            partial.load(str(self.known_hosts))
        self.assertIsNotNone(
            partial.lookup(HOST),
            "前提: paramiko は読めないバイトより前の東京-sw を読めている")

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

    def test_different_key_is_refused(self):
        """保存済みの鍵と違う相手（鍵 B）へ認証を送らず、ファイルも書き換えないこと。"""
        for title, data in self._layouts().items():
            with self.subTest(title):
                self._write(data)

                returned, errors, auth, _ = self._connect(self.key_b)

                self.assertFalse(returned)
                self.assertEqual(auth, [],
                                 "保存済みの鍵と違う相手へ認証が届いている")
                self.assertTrue(
                    any("ホストキーが変更されています" in e for e in errors),
                    errors)
                self.assertEqual(self.known_hosts.read_bytes(), data)

    def test_saved_key_is_accepted_without_warnings(self):
        """保存済みの鍵 A の相手には、既知の機器として認証へ進むこと。"""
        for title, data in self._layouts().items():
            with self.subTest(title):
                self._write(data)

                _, errors, auth, outputs = self._connect(self.key_a)

                self.assertEqual(auth, [("admin", "s3cret")], errors)
                self.assertEqual([o for o in outputs if "警告" in o], [])
                self.assertEqual(self.known_hosts.read_bytes(), data)

    def test_lines_read_before_the_decode_error_are_kept(self):
        """読めないバイトより前に paramiko が読めた行は、読み直しに頼らず client に残ること。

        読み直し（load_known_hosts）を何もしないものに替えて、読み直しの
        読み方によらず、先に読めた東京-sw の鍵が照合に使われることを見る。
        """
        from core import ssh_connection
        for title, data in self._layouts().items():
            with self.subTest(title):
                self._write(data)
                client = paramiko.SSHClient()
                self.addCleanup(client.close)

                with mock.patch.object(ssh_connection, "load_known_hosts"):
                    ssh_connection._load_known_hosts_into_client(
                        client, self.known_hosts, [])

                found = client.get_host_keys().lookup(HOST)
                self.assertIsNotNone(found, "先に読めた東京-sw の鍵を捨てている")
                self.assertEqual(found[self.key_a.get_name()], self.key_a)
                self.assertIsNone(client._host_keys_filename)


if __name__ == "__main__":
    unittest.main()
