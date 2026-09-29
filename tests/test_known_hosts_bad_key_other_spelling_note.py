"""別の綴りの行の鍵で「ホストキーが変更されています」になったとき、その行を案内に載せることを検証する。

何が起きていたか（453c72d / e53d27f で実測）。完全一致の鍵が無いとき、
大文字小文字やポートの書き方（"[host]:022" など）が違う行の鍵も照合に
使うようにしたが、その鍵と相手の鍵が食い違ったときの案内は今までどおり

    ホストキーが変更されています(中間者攻撃の可能性)。意図的な変更の場合は
    ~/.netbelt/known_hosts の該当ホスト行を削除してください。

だけだった。known_hosts には接続先の綴りの行が無いので、機器を入れ替えた
利用者は消すべき行（"[127.0.0.1]:022"・"SW1.EXAMPLE.COM"・ハッシュ化行）を
見つけにくい。保存直前の食い違い確認（a18138e）は、別の綴りの行も同じ
接続先の行であることを案内に足している。

どう直したか。読み替えに使った行の名前（known_hosts に書かれた綴り）を
覚えておき、食い違ったときは案内の末尾にその名前を足す。完全一致の行で
食い違ったときの案内は今までどおり。

その後、旧版が "[host]:22" で保存した行を ssh-keygen -H でハッシュ化した
形（|1|…）で、案内がファイルに無い平文の名前を載せていた（b2858c4 で実測:
案内の末尾が『…次の行です（同じ接続先の行です）: [127.0.0.1]:22』で、
その名前はファイルに 0 件）。22 番の旧名の読み替えは paramiko の lookup
（ハッシュ化名とも照合する）で引くのに、返す名前は組み立てた平文の
"[host]:22" だったため。直し方: その読み替えでも、一致した行に書かれた
綴りを返す（ほかの綴りの読み替えと同じ）。

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
PLAIN_MESSAGE = ("ホストキーが変更されています(中間者攻撃の可能性)。"
                 "意図的な変更の場合は ~/.netbelt/known_hosts の該当ホスト行を"
                 "削除してください。")


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


class BadHostKeyOtherSpellingNoteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khnote-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _line(self, name, key):
        return "%s %s %s\n" % (name, key.get_name(), key.get_base64())

    def _refused_errors(self, text, host, port=22):
        """鍵 A の行を置き、鍵 B の相手へ繋いで断られたときのエラーを返す。"""
        self.known_hosts.write_text(text, encoding="utf-8")
        server = _LocalSSHServer(self.key_b)
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

        self.assertFalse(returned)
        self.assertEqual(server.auth_log, [],
                         "保存済みの鍵と違う相手へ認証が届いている: %r" % (errors,))
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text)
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith(PLAIN_MESSAGE), errors)
        return errors

    def test_other_spelling_line_is_named(self):
        """読み替えに使った行の名前（ファイルに書かれた綴り）を案内に載せること。"""
        hashed = HostKeys.hash_host(LOWER)
        layouts = {
            "legacy port spelling": ("[127.0.0.1]:022", "127.0.0.1", 22),
            "other case": (UPPER, LOWER, 22),
            "hashed lower name": (hashed, UPPER, 22),
            "other case with port": ("[%s]:2202" % UPPER, LOWER, 2202),
            "legacy [host]:22": ("[%s]:22" % LOWER, LOWER, 22),
        }
        for title, (name, host, port) in layouts.items():
            with self.subTest(title):
                errors = self._refused_errors(
                    self._line(name, self.key_a), host, port)
                note = errors[0][len(PLAIN_MESSAGE):]
                self.assertIn(name, note,
                              "読み替えに使った行を案内していない: %r" % errors)
                self.assertIn("大文字小文字やポートの書き方", note, errors)

    def test_hashed_legacy_port_22_line_is_named_as_written(self):
        """ハッシュ化した "[host]:22" の行は、ファイルにある綴り（|1|…）で案内すること。

        組み立てた平文の "[host]:22" はファイルに無いので、案内に載せない。
        """
        for title, host in (("address", "127.0.0.1"), ("name", LOWER)):
            with self.subTest(title):
                hashed = HostKeys.hash_host("[%s]:22" % host)
                errors = self._refused_errors(
                    self._line(hashed, self.key_a), host)
                note = errors[0][len(PLAIN_MESSAGE):]
                self.assertIn("大文字小文字やポートの書き方", note, errors)
                self.assertIn(hashed, note,
                              "ファイルに書かれた綴りを案内していない: %r"
                              % errors)
                self.assertNotIn("[%s]:22" % host, note,
                                 "ファイルに無い名前を案内している: %r"
                                 % errors)

    def test_exact_line_keeps_the_plain_message(self):
        """完全一致の行で食い違ったときの案内は今までどおりであること。"""
        text = self._line(LOWER, self.key_a) + self._line(UPPER, self.key_a)
        errors = self._refused_errors(text, LOWER)
        self.assertEqual(errors, [PLAIN_MESSAGE])


if __name__ == "__main__":
    unittest.main()
