"""初回接続の鍵を保存する直前の食い違い確認が、同じ接続先の別の綴りの行も見ることを検証する。

何が起きていたか（441ea02 と e53d27f で実測）。接続先の鍵の照合は、完全一致の
行が無いとき、大文字小文字だけが違う名前や、旧版が書いた "[host]:22"・
"[host]:022" などの綴りの行も使う（_use_other_spelling_keys）。ところが
保存の直前に錠の中でディスクを読み直す確認（_refuse_conflicting_host_key）は、
完全一致の名前でしか引いていなかった。

同じ機器へ 2 つの接続が同時に初回接続し、こちらが known_hosts を読んだあと、
TOFU で保存する前に、別の接続が「SW1.EXAMPLE.COM <鍵 A>」を保存した形を模すと、
host 'sw1.example.com' で鍵 B の相手へ繋いだとき、認証（admin / s3cret）が
相手へ届き、'sw1.example.com <鍵 B>' が書き足された。完全一致の
'sw1.example.com <鍵 A>' が保存された場合は『ホスト鍵が食い違います』で
止まる（対照）。

どう直したか。保存直前の確認でも、完全一致の鍵が無ければ、読み込みのときと
同じ規則（_add_other_spelling_keys）で別の綴りの行の鍵を足してから照合する。

別の接続が保存したことは、TOFU の入口（_TofuHostKeyPolicy.missing_host_key。
こちらが読み込みを終え、相手の鍵を受け取ったあとに paramiko が呼ぶ）で
ディスクへ 1 行足して模す。
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


class KnownHostsSaveConflictOtherSpellingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khrace-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _line(self, name, key):
        return "%s %s %s\n" % (name, key.get_name(), key.get_base64())

    def _connect_while_other_saves(self, host, port, other_line, presented):
        """こちらの TOFU の直前に、別の接続が other_line を保存したことにして繋ぐ。"""
        self.known_hosts.write_bytes(b"")
        server = _LocalSSHServer(presented)
        self.addCleanup(server.close)
        real_port = server.port

        def to_local_server(client, hostname, port):
            return [(socket.AF_INET, ("127.0.0.1", real_port))]

        from core.ssh_connection import SSHConnection, _TofuHostKeyPolicy
        original = _TofuHostKeyPolicy.missing_host_key
        known_hosts = self.known_hosts

        def other_connection_saved_first(policy, client, hostname, key):
            with open(str(known_hosts), "ab") as f:
                f.write(other_line.encode("ascii"))
            return original(policy, client, hostname, key)

        conn = SSHConnection(host, port, "admin", password="s3cret")
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        with mock.patch.object(paramiko.SSHClient, "_families_and_addresses",
                               to_local_server), \
                mock.patch.object(_TofuHostKeyPolicy, "missing_host_key",
                                  other_connection_saved_first):
            returned = conn.connect()
        conn.dispose()
        return returned, errors, list(server.auth_log)

    def test_other_spelling_saved_meanwhile_stops_the_connection(self):
        """別の綴りで別の鍵が保存されていたら、認証を送らず、上書きもしないこと。"""
        cases = [
            ("other case", "sw1.example.com", 22,
             self._line("SW1.EXAMPLE.COM", self.key_a)),
            ("legacy [host]:22", "127.0.0.1", 22,
             self._line("[127.0.0.1]:22", self.key_a)),
            ("legacy [host]:022", "127.0.0.1", 22,
             self._line("[127.0.0.1]:022", self.key_a)),
            ("other port spelling in other case", "sw1.example.com", 2202,
             self._line("[SW1.EXAMPLE.COM]:02202", self.key_a)),
            ("hashed lowercase name", "SW1.EXAMPLE.COM", 22,
             self._line(HostKeys.hash_host("sw1.example.com"), self.key_a)),
            ("exact name (as before)", "sw1.example.com", 22,
             self._line("sw1.example.com", self.key_a)),
        ]
        for title, host, port, other_line in cases:
            with self.subTest(title):
                returned, errors, auth = self._connect_while_other_saves(
                    host, port, other_line, self.key_b)

                self.assertFalse(returned)
                self.assertEqual(auth, [],
                                 "別の鍵が保存済みの相手へ認証が届いている")
                self.assertTrue(
                    any("ホスト鍵が食い違います" in e for e in errors), errors)
                # 別の綴りの行で止めたときは、その行も該当行だと案内する
                self.assertEqual(
                    any("同じ接続先の行です" in e for e in errors),
                    not title.startswith("exact name"), errors)
                self.assertEqual(
                    self.known_hosts.read_text(encoding="ascii"), other_line)

    def test_same_key_saved_meanwhile_is_accepted(self):
        """別の綴りで同じ鍵が保存されていたら、今までどおり接続名で保存して進むこと。"""
        other_line = self._line("SW1.EXAMPLE.COM", self.key_b)
        returned, errors, auth = self._connect_while_other_saves(
            "sw1.example.com", 22, other_line, self.key_b)

        self.assertEqual(auth, [("admin", "s3cret")], errors)
        self.assertEqual(self.known_hosts.read_text(encoding="ascii"),
                         other_line + self._line("sw1.example.com", self.key_b))

    def test_other_endpoint_saved_meanwhile_is_not_a_conflict(self):
        """別のポートの行は同じ接続先ではないので、今までどおり TOFU で保存すること。"""
        other_line = self._line("[sw1.example.com]:2202", self.key_a)
        returned, errors, auth = self._connect_while_other_saves(
            "sw1.example.com", 22, other_line, self.key_b)

        self.assertEqual(auth, [("admin", "s3cret")], errors)
        self.assertEqual(self.known_hosts.read_text(encoding="ascii"),
                         other_line + self._line("sw1.example.com", self.key_b))


if __name__ == "__main__":
    unittest.main()
