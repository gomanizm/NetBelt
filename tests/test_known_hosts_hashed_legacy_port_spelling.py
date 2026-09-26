"""旧版の綴りのポートで保存され、あとでハッシュ化された known_hosts の行でも、相手の鍵を確かめることを検証する。

何が起きていたか（基準 c2bb66a で実測）。ポートを整数へそろえる前の版は、
config.json のポートの文字列をそのまま paramiko へ渡していたので、
"[host]:022" のような名前で known_hosts に書いた。平文のままならいまの版も
ポートを整数にそろえて照合する（test_ssh_known_hosts_legacy_port_spelling.py）。
ところが利用者がそのファイルを ssh-keygen -H などでハッシュ化していると、
ハッシュ化名（|1|salt|hash）はポートを読み出せないので、照合の候補
（"host" と "[host]:22"）に hash_host を掛け直して比べるしかない。connect() が
ポートを整数の 22 に置き換えたあとで候補を作るので、"[host]:022" という綴りは
候補に入らず、config.json が port "022" のままでもその行は照合に使われなかった:

    known_hosts "HostKeys.hash_host('[127.0.0.1]:022') <鍵A>"、port "022"、
    相手は鍵B → 認証 ('admin', 's3cret') が届き、"127.0.0.1 <鍵B>" が増える
    "hash_host('[127.0.0.1]:02202') <鍵A>"、port "02202" → 同じ
    （"[127.0.0.1]:2202 <鍵B>" が増える）
    鍵欄が壊れた同じハッシュ化行 → 「名前欄がこの機器の接続先と完全には
        一致しない」という警告だけで TOFU へ進み、認証が届く
    読み込みのあと保存の直前に、別の接続が同じハッシュ化行 <鍵A> を保存した
        → 保存直前の食い違い確認も同じ候補で引くので気づかず、認証が届き、
        鍵B の行が書き足される

どう直したか。connect() がポートを整数へそろえる前の設定の値を覚えておき、
ハッシュ化名の照合の候補に、旧版の paramiko がその値から組み立てた名前
（22 と等しくなければ "[host]:<設定の値>"）も加える。設定の値を整数に
そろえると接続先のポートになるときだけ使う。読み込みの照合・読めない行の
振り分け・保存直前の食い違い確認の 3 か所が同じ候補を使う。
設定を整数の 22 に直したあとは、ハッシュ化名から元の綴りを取り戻せないので
この行は照合に使えない（OpenSSH も同じ。残る制限）。
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


class HashedLegacyPortSpellingKnownHostsTest(unittest.TestCase):
    """旧版の綴りの名前をハッシュ化した行を置き、相手の鍵とポートの値を変えて繋ぐ。

    接続先の解決だけを差し替えて、このテストのサーバのポートへ向ける。
    ポートの値（config.json の値そのまま）は SSHConnection へそのまま渡す。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khhashspell-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _connect(self, known_hosts_text, presented_key, port, host=HOST,
                 saved_meanwhile=None):
        """known_hosts を置き、presented_key の相手へ port で繋ぐ。

        saved_meanwhile があれば、こちらの TOFU の直前（読み込みのあと、
        保存の前）に、別の接続がその行を保存したことにする。
        """
        self.known_hosts.write_text(known_hosts_text, encoding="utf-8")
        server = _LocalSSHServer(presented_key)
        self.addCleanup(server.close)
        real_port = server.port

        def to_local_server(client, hostname, port):
            return [(socket.AF_INET, ("127.0.0.1", real_port))]

        from core.ssh_connection import SSHConnection, _TofuHostKeyPolicy
        original = _TofuHostKeyPolicy.missing_host_key
        known_hosts = self.known_hosts

        def other_connection_saved_first(policy, client, hostname, key):
            if saved_meanwhile is not None:
                with open(str(known_hosts), "ab") as f:
                    f.write(saved_meanwhile.encode("ascii"))
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

    def _assert_refused(self, text, port, host=HOST):
        returned, errors, auth_log = self._connect(text, self.key_b, port,
                                                   host=host)
        self.assertFalse(returned)
        self.assertEqual(auth_log, [],
                         "保存済みの鍵と違う相手へ認証が届いている: %r" % (errors,))
        self.assertTrue(any("ホストキーが変更されています" in e for e in errors),
                        "鍵の食い違いとして中止していない: %r" % (errors,))
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text,
                         "known_hosts に別の鍵が書き足された")

    def test_hashed_zero_padded_port_line_refuses_other_key(self):
        """hash_host('[host]:022') <鍵A> で port "022"、鍵B の相手には認証を送らないこと。"""
        self._assert_refused(
            _line(HostKeys.hash_host("[%s]:022" % HOST), self.key_a), "022")

    def test_hashed_other_port_spelling_refuses_other_key(self):
        """22 番以外でも、hash_host('[host]:02202') <鍵A> で port "02202" の相手を確かめること。"""
        self._assert_refused(
            _line(HostKeys.hash_host("[%s]:02202" % HOST), self.key_a),
            "02202")

    def test_hashed_lowercase_legacy_name_refuses_other_case_host(self):
        """OpenSSH と同じく小文字でハッシュ化した旧綴りの行も、大文字を含む設定で照合すること。"""
        self._assert_refused(
            _line(HostKeys.hash_host("[sw1.example.com]:022"), self.key_a),
            "022", host="SW1.example.com")

    def test_hashed_legacy_spelling_accepts_the_same_key_without_writing(self):
        """鍵A の相手には普通に認証へ進み、読み替えた鍵をディスクに書かないこと。"""
        text = _line(HostKeys.hash_host("[%s]:022" % HOST), self.key_a)
        returned, errors, auth_log = self._connect(text, self.key_a, "022")

        self.assertEqual(auth_log, [("admin", "s3cret")],
                         "保存済みの鍵と同じ相手なのに認証へ進んでいない: %r"
                         % (errors,))
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text,
                         "読み替えた鍵がディスクへ書かれた")

    def test_broken_hashed_legacy_line_stops_the_connection(self):
        """鍵欄が壊れた hash_host('[host]:022') の行も、この機器の読めない行として中止すること。"""
        text = "%s ecdsa-sha2-nistp256 AAAA\n" % HostKeys.hash_host(
            "[%s]:022" % HOST)
        returned, errors, auth_log = self._connect(text, self.key_b, "022")

        self.assertFalse(returned)
        self.assertEqual(auth_log, [], "認証が相手へ届いている: %r" % (errors,))
        self.assertTrue(any("known_hosts に読めない行があり" in e
                            and "1 行目" in e for e in errors),
                        "読めない行として中止していない: %r" % (errors,))
        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), text)

    def test_hashed_legacy_line_saved_meanwhile_stops_the_connection(self):
        """保存の直前に別の接続が同じハッシュ化行 <鍵A> を保存していたら、認証を送らず上書きもしないこと。"""
        other_line = _line(HostKeys.hash_host("[%s]:022" % HOST), self.key_a)
        returned, errors, auth_log = self._connect(
            "", self.key_b, "022", saved_meanwhile=other_line)

        self.assertFalse(returned)
        self.assertEqual(auth_log, [],
                         "別の鍵が保存済みの相手へ認証が届いている: %r" % (errors,))
        self.assertTrue(any("ホスト鍵が食い違います" in e for e in errors),
                        errors)
        self.assertEqual(self.known_hosts.read_text(encoding="ascii"),
                         other_line)

    def test_hashed_legacy_spelling_of_another_port_is_not_used(self):
        """port 2202 は hash_host('[host]:022') の行を使わず、今までどおり初回接続になること。"""
        text = _line(HostKeys.hash_host("[%s]:022" % HOST), self.key_a)
        returned, errors, auth_log = self._connect(text, self.key_b, 2202)

        self.assertEqual(auth_log, [("admin", "s3cret")], errors)
        self.assertIn("[%s]:2202" % HOST,
                      self.known_hosts.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
