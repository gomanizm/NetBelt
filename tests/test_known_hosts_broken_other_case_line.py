"""大文字小文字だけが違う名前の読めない行は、完全一致の鍵が無いときだけ接続を中止することを検証する。

何が起きていたか（e53d27f で実測）。照合で大文字小文字を区別しないように
したとき（完全一致の鍵が無いときだけ）、読めない行の振り分けは完全一致の
鍵の有無を見ずに大文字小文字を無視していた。そのため

    SW1.EXAMPLE.COM ecdsa-sha2-nistp256 AAAA      （読めない行）
    sw1.example.com <鍵B>

で host "sw1.example.com" の正しい機器（鍵 B）へ繋ぐと、『known_hosts に
読めない行があり、sw1.example.com の鍵を検証できないため接続を中止しました』で
繋がらなくなった。441ea02 は警告を出したうえで、完全一致の鍵 B で検証して
認証へ進んでいた。完全一致の鍵があれば、大文字小文字だけが違う行は読めていても
照合に使われないので、中止しても防げるものは無い。

どう直したか。読めない行の振り分けでも、大文字小文字を区別しないのは、
照合と同じく完全一致の鍵（22 番は旧版の "[host]:22" の鍵も）が無いときだけに
した。完全一致の名前・旧版のポートの綴り（"[host]:22"・"[host]:022"）・
空白で割れた行は、今までどおり完全一致の鍵があっても中止する。

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


def _broken(name):
    """鍵欄が壊れた読めない行"""
    return "%s ecdsa-sha2-nistp256 AAAA\n" % name


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


class KnownHostsBrokenOtherCaseLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khbrokencase-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _line(self, name, key):
        return "%s %s %s\n" % (name, key.get_name(), key.get_base64())

    def _connect(self, text, presented_key, host, port=22):
        """known_hosts を置き、presented_key の相手へ host:port で繋ぐ。"""
        self.known_hosts.write_text(text, encoding="utf-8")
        server = _LocalSSHServer(presented_key)
        self.addCleanup(server.close)
        real_port = server.port

        def to_local_server(client, hostname, port):
            return [(socket.AF_INET, ("127.0.0.1", real_port))]

        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host, port, "admin", password="s3cret")
        self.addCleanup(conn.deleteLater)
        errors, outputs = [], []
        conn.error_occurred.connect(errors.append)
        conn.output_received.connect(outputs.append)
        with mock.patch.object(paramiko.SSHClient, "_families_and_addresses",
                               to_local_server):
            returned = conn.connect()
        conn.dispose()
        return returned, errors, list(server.auth_log), outputs

    def _assert_stopped(self, text, host, port=22):
        """読めない行を理由に、認証を送らずに中止すること。"""
        returned, errors, auth, _ = self._connect(text, self.key_b, host, port)
        self.assertFalse(returned)
        self.assertEqual(auth, [], "認証が相手へ届いている: %r" % (errors,))
        self.assertTrue(any("known_hosts に読めない行があり" in e
                            for e in errors), errors)
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text)

    def test_exact_line_verifies_despite_broken_other_case_line(self):
        """完全一致の鍵 B があれば、大文字小文字だけ違う読めない行は警告だけで検証へ進むこと。"""
        layouts = {
            "plain upper": (
                _broken(UPPER) + self._line(LOWER, self.key_b), LOWER, 22),
            "bracketed port 22 in upper": (
                _broken("[%s]:22" % UPPER) + self._line(LOWER, self.key_b),
                LOWER, 22),
            "hashed lower name, upper host": (
                _broken(HostKeys.hash_host(LOWER))
                + self._line(UPPER, self.key_b), UPPER, 22),
            "port 2202": (
                _broken("[%s]:2202" % UPPER)
                + self._line("[%s]:2202" % LOWER, self.key_b), LOWER, 2202),
        }
        for title, (text, host, port) in layouts.items():
            with self.subTest(title):
                returned, errors, auth, outputs = self._connect(
                    text, self.key_b, host, port)

                self.assertEqual(auth, [("admin", "s3cret")],
                                 "検証できる機器が繋がらない: %r" % (errors,))
                self.assertFalse(any("読めない行があり" in e for e in errors),
                                 errors)
                self.assertTrue(any("読めない行があります" in o
                                    for o in outputs),
                                "読めない行を知らせていない: %r" % (outputs,))
                self.assertEqual(
                    self.known_hosts.read_text(encoding="utf-8"), text)

    def test_legacy_port22_line_verifies_despite_broken_other_case_line(self):
        """22 番で旧版の "[host]:22" の鍵 B があるときも、警告だけで検証へ進むこと。"""
        text = _broken(UPPER) + self._line("[%s]:22" % LOWER, self.key_b)
        returned, errors, auth, outputs = self._connect(
            text, self.key_b, LOWER)

        self.assertEqual(auth, [("admin", "s3cret")], errors)
        self.assertTrue(any("読めない行があります" in o for o in outputs),
                        outputs)
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text)

    def test_exact_line_still_refuses_other_key(self):
        """完全一致の鍵 A があり、相手が鍵 B なら、今までどおり認証を送らないこと。"""
        text = _broken(UPPER) + self._line(LOWER, self.key_a)
        returned, errors, auth, outputs = self._connect(
            text, self.key_b, LOWER)

        self.assertFalse(returned)
        self.assertEqual(auth, [], "認証が相手へ届いている: %r" % (errors,))
        self.assertTrue(any("ホストキーが変更されています" in e
                            for e in errors), errors)
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text)

    def test_broken_other_case_line_stops_without_exact_line(self):
        """完全一致の鍵が無ければ（別の機器の鍵だけなら）、今までどおり中止すること。"""
        self._assert_stopped(
            _broken(UPPER) + self._line("sw2.example.com", self.key_b), LOWER)

    def test_broken_same_case_lines_still_stop_with_exact_line(self):
        """完全一致の鍵があっても、同じ綴りの読めない行は今までどおり中止すること。

        旧版のポートの綴り（"[host]:22"・"[host]:022"）や、" 22" で名前欄が
        割れた行も、大文字小文字まで同じならこの機器の行として中止する。
        """
        exact = self._line(LOWER, self.key_b)
        layouts = {
            "exact name": _broken(LOWER) + exact,
            "legacy [host]:22": _broken("[%s]:22" % LOWER) + exact,
            "legacy [host]:022": _broken("[%s]:022" % LOWER) + exact,
            "split [host]: 22": "[%s]: 22 %s %s\n%s" % (
                LOWER, self.key_a.get_name(), self.key_a.get_base64(), exact),
        }
        for title, text in layouts.items():
            with self.subTest(title):
                self._assert_stopped(text, LOWER)


if __name__ == "__main__":
    unittest.main()
