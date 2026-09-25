"""旧版が "[host]:22" の名前で保存した鍵でも、22 番の相手を確かめることを検証する。

何が起きていたか（基準 028ebc2 で実測）。4f472da で SSH のポートを整数へ
そろえたため、config.json の "port" が文字列 "22" の機器も known_hosts を
"host" の名前で引くようになった。ところが、それ以前の版で "22" のまま
繋いでいた機器は、paramiko 4.0.0 が `port == 22`（整数との比較）で名前を
決めていたため、known_hosts に "[host]:22 <鍵>" の行しか持っていない。
更新後の最初の接続ではこの行が使われず、TOFU が黙って別の鍵を受け入れ、
パスワードが相手へ届いていた。

    known_hosts: [127.0.0.1]:22 <鍵 A> だけ、127.0.0.1:22 の相手は鍵 B
    port="22" でも 22 でも → 相手へパスワード認証が届く
                             （('admin', 's3cret')）、known_hosts に
                             "127.0.0.1 <鍵 B>" が増えて以後の正になる
    名前をハッシュ化した行（|1|…）でも同じ

どう直したか。SSHConnection.connect で known_hosts を読み込んだ直後に、
22 番のときだけ、"host" の鍵が無く "[host]:22" の鍵があれば、それを
"host" の鍵としてメモリ上で読み替える（ディスクには書かない）。"host" の
行があるときはそちらを使い、読み替えは足さない。22 番以外のポートは
今までどおり "[host]:port" で引く。
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


def _line(name, key):
    return "%s %s %s\n" % (name, key.get_name(), key.get_base64())


class LegacyPort22KnownHostsTest(unittest.TestCase):
    """known_hosts の中身と相手の鍵を変え、22 番へ繋いだときの止まり方を見る。

    22 番で待ち受けるとほかのものとぶつかるので、paramiko の接続先の
    解決だけを差し替えて、このテストのサーバのポートへ向ける。ポート番号の
    値は、そのまま paramiko へ渡る（known_hosts を引く名前はそこで決まる）。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-legacy22-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _connect(self, known_hosts_text, presented_key, port):
        """known_hosts を置き、presented_key の相手へ port で繋ぐ。"""
        self.known_hosts.write_text(known_hosts_text, encoding="utf-8")
        server = _LocalSSHServer(presented_key)
        self.addCleanup(server.close)
        real_port = server.port

        def to_local_server(client, hostname, port):
            return [(socket.AF_INET, ("127.0.0.1", real_port))]

        from core.ssh_connection import SSHConnection
        conn = SSHConnection("127.0.0.1", port, "admin", password="s3cret")
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        with mock.patch.object(paramiko.SSHClient, "_families_and_addresses",
                               to_local_server):
            returned = conn.connect()
        conn.dispose()
        return returned, errors, server.auth_log

    def _assert_refused(self, returned, errors, auth_log):
        self.assertFalse(returned)
        self.assertEqual(auth_log, [],
                         "保存済みの鍵と違う相手へ認証が届いている")
        self.assertTrue(any("ホストキーが変更されています" in e for e in errors),
                        "鍵の食い違いとして中止していない: %r" % (errors,))

    def test_legacy_line_refuses_other_key_with_string_port(self):
        """"[host]:22 <鍵A>" だけで port "22"、鍵B の相手には認証を送らないこと。"""
        text = _line("[127.0.0.1]:22", self.key_a)
        self._assert_refused(*self._connect(text, self.key_b, "22"))
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text,
                         "known_hosts に別の鍵が書き足された")

    def test_legacy_line_refuses_other_key_with_integer_port(self):
        """機器の編集で整数の 22 に直したあとも、旧行で確かめること。"""
        text = _line("[127.0.0.1]:22", self.key_a)
        self._assert_refused(*self._connect(text, self.key_b, 22))

    def test_hashed_legacy_line_refuses_other_key(self):
        """名前をハッシュ化した "[host]:22" の行でも、鍵B の相手を断ること。"""
        hashed = paramiko.hostkeys.HostKeys.hash_host("[127.0.0.1]:22")
        text = _line(hashed, self.key_a)
        self._assert_refused(*self._connect(text, self.key_b, "22"))

    def test_legacy_line_accepts_the_same_key_without_writing(self):
        """鍵A の相手には普通に認証へ進み、ディスクには何も書かないこと。"""
        text = _line("[127.0.0.1]:22", self.key_a)
        returned, errors, auth_log = self._connect(text, self.key_a, "22")

        self.assertEqual(auth_log, [("admin", "s3cret")],
                         "保存済みの鍵と同じ相手なのに認証へ進んでいない: %r"
                         % (errors,))
        self.assertFalse(any("ホストキーが変更されています" in e
                             for e in errors), errors)
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text,
                         "読み替えた鍵がディスクへ書かれた")

    def test_host_line_wins_over_legacy_line(self):
        """"host" の行があるときは、そちらだけで確かめること（読み替えを足さない）。"""
        text = (_line("127.0.0.1", self.key_a)
                + _line("[127.0.0.1]:22", self.key_b))
        self._assert_refused(*self._connect(text, self.key_b, "22"))

    def test_host_line_same_key_still_connects(self):
        """"host" の行と同じ鍵の相手は、旧行が別の鍵でも認証へ進むこと。"""
        text = (_line("127.0.0.1", self.key_a)
                + _line("[127.0.0.1]:22", self.key_b))
        returned, errors, auth_log = self._connect(text, self.key_a, 22)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)

    def test_other_ports_do_not_use_the_port22_line(self):
        """22 番以外のポートは "[host]:22" の行を使わないこと（今までどおり）。"""
        text = _line("[127.0.0.1]:22", self.key_a)
        returned, errors, auth_log = self._connect(text, self.key_b, 2202)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)
        self.assertIn("[127.0.0.1]:2202",
                      self.known_hosts.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
