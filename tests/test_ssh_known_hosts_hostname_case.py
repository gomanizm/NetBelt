"""ホスト名の大文字小文字だけが違う known_hosts の行でも、相手の鍵を確かめることを検証する。

何が起きていたか（基準 441ea02 で実測）。paramiko 4.0.0 は known_hosts の名前を
文字列のまま比べ、ハッシュ化名（|1|salt|hash）も渡された綴りのまま掛け直す。
NetBelt は機器の設定のホスト名をそのまま渡していた。OpenSSH はホスト名を
小文字にしてから照合し、書き込み（ハッシュ化も）も小文字で行うので、
OpenSSH から写した行と大文字を含む接続先の組み合わせや、機器の編集で
大文字小文字だけを変えた機器では、保存済みの鍵が無いものとして TOFU が
黙って受け入れ、鍵が変わっていても相手へパスワードが届いていた:

    known_hosts "localhost <鍵A>"、host "LOCALHOST"、相手は鍵B
        → 認証 ('admin', 's3cret') が届き、"LOCALHOST <鍵B>" が増える
    "LocalHost <鍵A>" と host "localhost"                     → 同じ
    小文字から作ったハッシュ化行 <鍵A> と host "LOCALHOST"    → 同じ
    "[localhost]:2202 <鍵A>" と host "LOCALHOST"、2202 番     → 同じ

どう直したか。照合で大文字小文字を区別しない（OpenSSH と同じ）。ただし
完全一致の鍵が無いときだけで、完全一致の行があれば今までどおりそれだけを
使う。平文の名前は小文字にそろえて比べ、ハッシュ化名は小文字にそろえた
接続先名でも掛け直して比べる。見つかった行の鍵は、接続先名の鍵として
メモリ上で足すだけで、known_hosts には書かない。初回接続で保存する名前も
今までどおり設定の綴りのまま（小文字へ変えると、既存の大文字を含む行と
食い違う）。

読めない行の振り分けも同じ規則にそろえた。大文字小文字だけが違う名前の
読めない行は、この機器の行として接続を中止する。読めていればこの機器の
照合に使われる行なので、旧版の "[host]:22" の壊れた行を中止しているのと
同じ扱いにする（警告だけで進むと、その行が読めていれば止まった相手へ
TOFU で認証が届く）。

名前解決は差し替えて localhost のテスト用サーバへ向けるので、例の
ホスト名（sw1.example.com）を実際に引くことはない。
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

LOWER = "sw1.example.com"
UPPER = "SW1.EXAMPLE.COM"


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


class KnownHostsHostnameCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khcase-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _connect(self, known_hosts_text, presented_key, host, port=22):
        """known_hosts を置き、presented_key の相手へ host:port で繋ぐ。"""
        if known_hosts_text is not None:
            self.known_hosts.write_text(known_hosts_text, encoding="utf-8")
        server = _LocalSSHServer(presented_key)
        self.addCleanup(server.close)
        real_port = server.port

        def to_local_server(client, hostname, port):
            return [(socket.AF_INET, ("127.0.0.1", real_port))]

        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host, port, "admin", password="s3cret")
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        with mock.patch.object(paramiko.SSHClient, "_families_and_addresses",
                               to_local_server):
            returned = conn.connect()
        conn.dispose()
        return returned, errors, server.auth_log

    def _assert_refused(self, text, host, port=22):
        returned, errors, auth_log = self._connect(text, self.key_b, host, port)
        self.assertFalse(returned)
        self.assertEqual(auth_log, [],
                         "保存済みの鍵と違う相手へ認証が届いている: %r" % (errors,))
        self.assertTrue(any("ホストキーが変更されています" in e for e in errors),
                        "鍵の食い違いとして中止していない: %r" % (errors,))
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text,
                         "known_hosts に別の鍵が書き足された")

    def test_lowercase_line_refuses_other_key_for_uppercase_host(self):
        """"sw1.example.com <鍵A>" で host "SW1.EXAMPLE.COM"、鍵B の相手を断ること。"""
        self._assert_refused(_line(LOWER, self.key_a), UPPER)

    def test_mixed_case_line_refuses_other_key_for_lowercase_host(self):
        """逆向き（行が大文字を含み、設定が小文字）でも断ること。"""
        self._assert_refused(_line("SW1.Example.com", self.key_a), LOWER)

    def test_openssh_hashed_line_refuses_other_key(self):
        """OpenSSH が小文字から作ったハッシュ化行でも、大文字の設定で断ること。"""
        self._assert_refused(_line(HostKeys.hash_host(LOWER), self.key_a), UPPER)

    def test_bracketed_port_line_refuses_other_key(self):
        """"[sw1.example.com]:2202 <鍵A>" で host "SW1.EXAMPLE.COM"、2202 番を断ること。"""
        self._assert_refused(_line("[%s]:2202" % LOWER, self.key_a), UPPER, 2202)

    def test_hashed_bracketed_port_line_refuses_other_key(self):
        """OpenSSH がハッシュ化した "[sw1.example.com]:2202" の行でも断ること。"""
        self._assert_refused(
            _line(HostKeys.hash_host("[%s]:2202" % LOWER), self.key_a),
            UPPER, 2202)

    def test_legacy_port_spelling_in_other_case_refuses_other_key(self):
        """旧版の綴り "[SW1.EXAMPLE.COM]:022" の行も、小文字の設定の 22 番で断ること。"""
        self._assert_refused(_line("[%s]:022" % UPPER, self.key_a), LOWER)

    def test_exact_line_wins_over_other_case(self):
        """完全一致の行があれば、大文字小文字だけ違う行（別の鍵）は使わないこと。"""
        text = _line(UPPER, self.key_b) + _line(LOWER, self.key_a)
        returned, errors, auth_log = self._connect(text, self.key_b, UPPER)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text)

    def test_same_key_connects_without_writing(self):
        """同じ鍵の相手は認証へ進み、読み替えた名前をディスクに書かないこと。"""
        text = _line(LOWER, self.key_a)
        returned, errors, auth_log = self._connect(text, self.key_a, UPPER)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text,
                         "読み替えた鍵がディスクへ書かれた")

    def test_first_connection_saves_the_configured_spelling(self):
        """初回接続で保存する名前は、今までどおり設定の綴りのままであること。"""
        returned, errors, auth_log = self._connect(None, self.key_b,
                                                   "SW1.Example.COM")

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)
        names = [l.split()[0] for l in
                 self.known_hosts.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(names, ["SW1.Example.COM"])

    def test_other_port_does_not_use_the_plain_host_line(self):
        """2202 番は大文字小文字違いの "host" 行を使わず、今までどおり初回接続になること。"""
        text = _line(LOWER, self.key_a)
        returned, errors, auth_log = self._connect(text, self.key_b, UPPER, 2202)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)
        self.assertIn("[%s]:2202" % UPPER,
                      self.known_hosts.read_text(encoding="utf-8"))

    def test_broken_line_in_other_case_stops_the_connection(self):
        """大文字小文字だけ違う名前の読めない行は、この機器の行として中止すること。"""
        text = "%s ecdsa-sha2-nistp256 AAAA\n" % UPPER
        returned, errors, auth_log = self._connect(text, self.key_b, LOWER)

        self.assertFalse(returned)
        self.assertEqual(auth_log, [], "認証が相手へ届いている: %r" % (errors,))
        self.assertTrue(any("known_hosts に読めない行があり" in e
                            for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
