"""ハッシュ化した行を含む known_hosts の読み込みが、行数の 2 乗で重くならないことを検証する。

何が起きていたか（基準 441ea02 で実測）。壊れた行も BOM も無い known_hosts は
paramiko の HostKeys.load で読んでいた。paramiko 4.0.0 の HostKeys.load は
1 行読むたびに self.check() → lookup を呼び、それまでに読んだハッシュ化名
（|1|salt|hash）の行すべてに hash_host（HMAC-SHA1）を掛け直す。そのため
「ハッシュ化行より後ろにある平文行」の数 × ハッシュ化行の数だけ計算が要る。

    平文とハッシュ化を交互に 400 行 : _setup_host_keys の hash_host 20,300 回
    同じ形で 2,000 行               : 506,500 回・6.5 秒
    （平文だけ 2,000 行は 0 回・0.3 秒、全部ハッシュ化 2,000 行は 0.4 秒）

OpenSSH の known_hosts（既定で全行ハッシュ化）を写し、そのあとへ NetBelt が
平文の行を足していく使い方で起きる。この読み込みは接続スレッドで、しかも
known_hosts の錠の中で動くので、同時に繋ぐ接続や鍵の保存がその間ずっと待つ。

どう直したか。ハッシュ化名の行（|1|）があるファイルは、paramiko の
HostKeys.load の読み方（既定の文字コード・text モードの改行）のまま、1 行ごとの
重複の判定（check）だけを名前の文字列の比較に替えた HostKeys
（_HostKeysLoadedLinearly）へ読み込む。重複の判定でハッシュ化名に hash_host を
掛け直さないので、読み込みは行数に比例する。読み方は paramiko と同じなので、
CR だけの改行のファイルも全行を読める（自前のローダ load_known_hosts は LF で
しか行を分けないので、そちらへ回すと 2 行目以降を読み落とす）。平文だけの
ファイルは今までどおり paramiko の client.load_host_keys に読ませる。

呼び出し回数で見るので、CPU の混雑や GC の止まりで結果は揺れない。
"""
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

LINES = 400


def _host(i):
    return "198.51.100.%d" % (i % 250 + 1) if i < 250 else \
        "203.0.113.%d" % (i - 250 + 1)


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


class KnownHostsLoadCostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khcost-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _line(self, name, key):
        return "%s %s %s\n" % (name, key.get_name(), key.get_base64())

    def _write_alternating(self):
        """平文とハッシュ化の行を交互に LINES 行置く（偶数番目がハッシュ化）。"""
        text = "".join(
            self._line(HostKeys.hash_host(_host(i)) if i % 2 == 0 else _host(i),
                       self.key_a)
            for i in range(LINES))
        self.known_hosts.write_text(text, encoding="utf-8")

    def _setup_host_keys(self):
        """本物の SSHClient に _setup_host_keys を通し、その client を返す。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        self.addCleanup(conn.deleteLater)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return client

    def test_hash_host_calls_grow_linearly(self):
        """交互に並べた 400 行で、hash_host の呼び出しが行数の 4 倍以下であること。"""
        self._write_alternating()
        calls = []
        real_hash_host = HostKeys.hash_host

        def counting(hostname, salt=None):
            calls.append(hostname)
            return real_hash_host(hostname, salt)

        with mock.patch.object(HostKeys, "hash_host", staticmethod(counting)):
            self._setup_host_keys()

        self.assertLessEqual(
            len(calls), LINES * 4,
            "known_hosts %d 行の読み込みで hash_host を %d 回呼んでいる"
            "（行数の 2 乗で増える読み方をしている）" % (LINES, len(calls)))

    def test_every_line_is_still_looked_up(self):
        """先頭・中ほど・末尾の接続先（平文とハッシュ化の両方）の鍵が引けること。"""
        self._write_alternating()
        keys = self._setup_host_keys().get_host_keys()

        for i in (0, 1, LINES // 2, LINES // 2 + 1, LINES - 2, LINES - 1):
            found = keys.lookup(_host(i))
            self.assertIsNotNone(found, "%d 行目の %s が引けない" % (i + 1, _host(i)))
            self.assertEqual(found[self.key_a.get_name()], self.key_a)
        self.assertIsNone(keys.lookup("192.0.2.200"))

    def test_hashed_line_still_refuses_another_key(self):
        """ハッシュ化行 A＋同じ鍵の平文行がある機器へ、鍵 B の相手が出たら認証を送らないこと。"""
        hashed = HostKeys.hash_host("127.0.0.1")
        text = (self._line(hashed, self.key_a)
                + self._line("198.51.100.7", self.key_a)
                + self._line("127.0.0.1", self.key_a))
        self.known_hosts.write_text(text, encoding="utf-8")
        server = _LocalSSHServer(self.key_b)
        self.addCleanup(server.close)
        real_port = server.port

        def to_local_server(client, hostname, port):
            return [(socket.AF_INET, ("127.0.0.1", real_port))]

        from core.ssh_connection import SSHConnection
        conn = SSHConnection("127.0.0.1", 22, "admin", password="s3cret")
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        with mock.patch.object(paramiko.SSHClient, "_families_and_addresses",
                               to_local_server):
            returned = conn.connect()
        conn.dispose()

        self.assertFalse(returned)
        self.assertEqual(server.auth_log, [],
                         "保存済みの鍵と違う相手へ認証が届いている")
        self.assertTrue(any("ホストキーが変更されています" in e for e in errors),
                        errors)
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text)

    def test_plain_file_is_still_read_by_paramiko(self):
        """ハッシュ化名の無いファイルは、今までどおり paramiko に読ませること。"""
        self.known_hosts.write_text(
            "".join(self._line(_host(i), self.key_a) for i in range(4)),
            encoding="utf-8")
        with mock.patch.object(paramiko.SSHClient, "load_host_keys",
                               autospec=True,
                               side_effect=paramiko.SSHClient.load_host_keys) as load:
            client = self._setup_host_keys()

        self.assertEqual(load.call_count, 1)
        self.assertIsNotNone(client.get_host_keys().lookup(_host(3)))

    def test_cr_only_file_with_hashed_names_reads_every_line(self):
        """CR だけの改行のファイルは、ハッシュ化名があっても全行が引けること。

        自前のローダは LF で行を分けるので、CR だけの改行だと 2 行目以降を
        読み落とす（paramiko は text モードで CR でも分ける）。
        """
        text = "".join(
            self._line(HostKeys.hash_host(_host(i)) if i == 0 else _host(i),
                       self.key_a).replace("\n", "\r")
            for i in range(4))
        self.known_hosts.write_bytes(text.encode("ascii"))
        keys = self._setup_host_keys().get_host_keys()

        for i in range(4):
            self.assertIsNotNone(keys.lookup(_host(i)),
                                 "%d 行目の %s が引けない" % (i + 1, _host(i)))


if __name__ == "__main__":
    unittest.main()
