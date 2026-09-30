"""旧版が "22" 以外の綴りのポートで残した known_hosts の行でも、相手の鍵を確かめることを検証する。

何が起きていたか（基準 441ea02 で実測）。ポートを整数へそろえる前の版は、
config.json のポートの文字列をそのまま paramiko へ渡していたので、
paramiko 4.0.0 は "[host]:<その文字列>" という名前で known_hosts に書いた。
いまの版はポートを整数にそろえて "host"（22 番）や "[host]:2202" で引くので、
"22" と綴った行（"[host]:22"）しか読み替えておらず、ほかの綴りの行は照合に
使われなかった。相手の鍵が変わっていても TOFU が黙って受け入れ、パスワードが
相手へ届いて、新しい鍵の行が以後の正として足された:

    known_hosts "[127.0.0.1]:022 <鍵A>"、port "022" でも 22 でも、相手は鍵B
        → 認証 ('admin', 's3cret') が届き、"127.0.0.1 <鍵B>" が増える
    "[127.0.0.1]:+22 <鍵A>" と port "+22"                 → 同じ
    "[127.0.0.1]:02202 <鍵A>" と port 2202                → 同じ（"[127.0.0.1]:2202 <鍵B>" が増える）
    旧版が " 22" で書いた "[127.0.0.1]: 22 <種別> <鍵A>"  → 名前欄が空白で割れた
        読めない行になり、「名前欄がこの機器の接続先と完全には一致しない」
        という警告だけで TOFU へ進み、認証が届く

どう直したか。完全一致の鍵が無く、今までの "[host]:22" の読み替えも効かない
ときだけ、平文の "[h]:p" のうち h が接続先と同じで、p を整数にそろえると
接続先のポートになる行の鍵を、接続先名の鍵としてメモリ上で読み替える
（ディスクには書かない。22 番に限らない）。名前欄が割れた "[host]: 22" の
読めない行は、割れた 2 つの欄をつないだ名前で振り分け、この機器の行なら
既存の読めない行と同じく接続を中止する（利用者の決定 (a)）。鍵欄が壊れた
"[host]:022" の行のように、読み替えの対象になる綴りの読めない行も同じく中止する
（"[host]:22" の壊れた行を中止しているのと同じ理由）。
22 番以外のポートが "[host]:22" や "host" の行を使わないのは今までどおり。
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

HOST = "127.0.0.1"


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


class LegacyPortSpellingKnownHostsTest(unittest.TestCase):
    """旧版の綴りの行を置き、相手の鍵とポートの値を変えて繋ぐ。

    接続先の解決だけを差し替えて、このテストのサーバのポートへ向ける。
    ポートの値は、そのまま known_hosts を引く名前を決める。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khspell-"))
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
        conn = SSHConnection(HOST, port, "admin", password="s3cret")
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        with mock.patch.object(paramiko.SSHClient, "_families_and_addresses",
                               to_local_server):
            returned = conn.connect()
        conn.dispose()
        return returned, errors, server.auth_log

    def _assert_refused(self, text, presented_key, port):
        returned, errors, auth_log = self._connect(text, presented_key, port)
        self.assertFalse(returned)
        self.assertEqual(auth_log, [],
                         "保存済みの鍵と違う相手へ認証が届いている: %r" % (errors,))
        self.assertTrue(any("ホストキーが変更されています" in e for e in errors),
                        "鍵の食い違いとして中止していない: %r" % (errors,))
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text,
                         "known_hosts に別の鍵が書き足された")

    def test_zero_padded_port_line_refuses_other_key(self):
        """"[host]:022 <鍵A>" で port "022"、鍵B の相手には認証を送らないこと。"""
        self._assert_refused(_line("[%s]:022" % HOST, self.key_a),
                             self.key_b, "022")

    def test_zero_padded_port_line_refuses_after_port_is_fixed(self):
        """機器の編集で整数の 22 に直したあとも、"[host]:022" の行で確かめること。"""
        self._assert_refused(_line("[%s]:022" % HOST, self.key_a),
                             self.key_b, 22)

    def test_plus_sign_port_line_refuses_other_key(self):
        """"[host]:+22 <鍵A>" で port "+22" でも、鍵B の相手を断ること。"""
        self._assert_refused(_line("[%s]:+22" % HOST, self.key_a),
                             self.key_b, "+22")

    def test_other_port_spelling_refuses_other_key(self):
        """22 番以外でも、"[host]:02202 <鍵A>" の行で 2202 番の相手を確かめること。"""
        self._assert_refused(_line("[%s]:02202" % HOST, self.key_a),
                             self.key_b, 2202)

    def test_split_space_port_line_stops_the_connection(self):
        """旧版が " 22" で書いた、名前欄が割れた行は、この機器の読めない行として中止すること。"""
        text = "[%s]: 22 %s %s\n" % (HOST, self.key_a.get_name(),
                                     self.key_a.get_base64())
        returned, errors, auth_log = self._connect(text, self.key_b, 22)

        self.assertFalse(returned)
        self.assertEqual(auth_log, [], "認証が相手へ届いている: %r" % (errors,))
        self.assertTrue(any("known_hosts に読めない行があり" in e
                            and "1 行目" in e for e in errors),
                        "読めない行として中止していない: %r" % (errors,))
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text)

    def test_broken_zero_padded_line_stops_the_connection(self):
        """鍵欄が壊れた "[host]:022" の行も、この機器の読めない行として中止すること。"""
        text = "[%s]:022 ecdsa-sha2-nistp256 AAAA\n" % HOST
        returned, errors, auth_log = self._connect(text, self.key_b, 22)

        self.assertFalse(returned)
        self.assertEqual(auth_log, [], "認証が相手へ届いている: %r" % (errors,))
        self.assertTrue(any("known_hosts に読めない行があり" in e
                            for e in errors), errors)

    def test_legacy_spelling_accepts_the_same_key_without_writing(self):
        """鍵A の相手には普通に認証へ進み、読み替えた鍵をディスクに書かないこと。"""
        text = _line("[%s]:022" % HOST, self.key_a)
        returned, errors, auth_log = self._connect(text, self.key_a, 22)

        self.assertEqual(auth_log, [("admin", "s3cret")],
                         "保存済みの鍵と同じ相手なのに認証へ進んでいない: %r"
                         % (errors,))
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text,
                         "読み替えた鍵がディスクへ書かれた")

    def test_exact_line_wins_over_legacy_spelling(self):
        """"host" の行があれば、別の鍵の "[host]:022" の行は使わないこと。"""
        text = (_line(HOST, self.key_b)
                + _line("[%s]:022" % HOST, self.key_a))
        returned, errors, auth_log = self._connect(text, self.key_b, 22)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)

    def test_port22_legacy_line_still_wins_over_other_spellings(self):
        """今までの "[host]:22" の読み替えが効くときは、ほかの綴りの行を使わないこと。"""
        text = (_line("[%s]:022" % HOST, self.key_a)
                + _line("[%s]:22" % HOST, self.key_b))
        returned, errors, auth_log = self._connect(text, self.key_b, 22)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)

    def test_other_port_does_not_use_legacy_spellings(self):
        """2202 番は "[host]:022" の行を使わず、今までどおり初回接続になること。"""
        text = _line("[%s]:022" % HOST, self.key_a)
        returned, errors, auth_log = self._connect(text, self.key_b, 2202)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)
        self.assertIn("[%s]:2202" % HOST,
                      self.known_hosts.read_text(encoding="utf-8"))

    def test_split_line_for_another_port_only_warns(self):
        """名前欄が割れた "[host]: 2202" の行は、22 番の接続では警告だけにすること。"""
        text = "[%s]: 2202 %s %s\n" % (HOST, self.key_a.get_name(),
                                       self.key_a.get_base64())
        returned, errors, auth_log = self._connect(text, self.key_b, 22)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)
        self.assertFalse(any("known_hosts に読めない行があり" in e
                             for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
