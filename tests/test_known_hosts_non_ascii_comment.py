"""known_hosts の注釈欄・コメント行に日本語が入っていると全機器が繋がらない件。

実測（429bf80 / paramiko 4.0.0 / Windows・cp932 環境。
locale.getpreferredencoding(False) → 'cp932'、sys.flags.utf8_mode → 0）:
鍵の行はどれも正しく読めるのに、注釈欄に「ラボ」のような日本語が
入っているだけで、すべての機器への接続が中止されていた。

  192.0.2.8 ecdsa-sha2-nistp256 AAAA... ラボ

このファイルで _setup_host_keys() を通すと:

  HostKeyStoreError: 既知ホスト鍵 (known_hosts) を読めないため接続を
  中止しました: 'cp932' codec can't decode byte 0x9c in position ...:
  illegal multibyte sequence

理由は読み方の食い違い。_iter_known_hosts_lines() はファイルを bytes で
読んで utf-8/replace でデコードするので「読めない行」は 0 件になり、
_load_known_hosts_into_client() は paramiko に読ませる経路を選ぶ。ところが
paramiko の HostKeys.load は open(filename, "r") で、既定エンコーディング
（この環境では cp932）で読む。そのため client.load_host_keys() が
UnicodeDecodeError を投げ、_setup_host_keys の except Exception が
HostKeyStoreError に包み直して、関係のない機器まで全部中止になっていた。
しかも案内は「ファイルを開けない（権限・排他・入出力エラー）か、中身を
読み取れない状態です」で、行番号も行の内容も出ない。

直し方: _load_known_hosts_into_client() で paramiko の読み込みを主経路の
まま残しつつ、UnicodeDecodeError だけを受け止めて、読める行を自前の
ローダ（load_known_hosts）で取り込む。paramiko の load_host_keys は
読む前に _host_keys_filename を覚えるので、この client が持っているのは
ファイルの一部だけだと分かるよう None に戻す（2d9394a 以降、保存は
_save_known_hosts が錠の中でディスクから作り直して行い、paramiko の
save_host_keys は保存経路から外れている）。
例外を Exception まで広げないのは、権限エラー（PermissionError）で
中止する既存の動きを残すため。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class KnownHostsNonAsciiCommentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khnonascii-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "known_hosts"

    def _line(self, hostname, key, annotation=""):
        text = "%s %s %s" % (hostname, key.get_name(), key.get_base64())
        return text + (" " + annotation if annotation else "")

    def _write_known_hosts(self, lines):
        # 利用者が書き足す注釈は UTF-8。cp932 では読めないバイトを含む
        self.path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    def _setup(self, host, port=22):
        """本物の SSHClient に _setup_host_keys を通し、画面に出た文を返す。"""
        import paramiko
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host=host, port=port, username="admin")
        self.addCleanup(conn.deleteLater)
        messages = []
        conn.output_received.connect(messages.append)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return client, messages

    def test_annotated_line_does_not_block_unrelated_hosts(self):
        """注釈に日本語がある行があっても、他の機器は繋がること。"""
        self._write_known_hosts(
            [self._line("192.0.2.8", self.key_a, "ラボ")])

        client, _messages = self._setup("192.0.2.5")

        self.assertIsNotNone(
            client.get_host_keys().get("192.0.2.8"),
            "読める行の鍵まで失われている（注釈の日本語で全機器が中止）")

    def test_annotated_host_stays_known(self):
        """注釈に日本語を付けた機器自身が「未知のホスト」に戻らないこと。"""
        self._write_known_hosts(
            [self._line("192.0.2.8", self.key_a, "予備機（待機）")])

        client, messages = self._setup("192.0.2.8")

        self.assertTrue(
            client.get_host_keys().check("192.0.2.8", self.key_a),
            "注釈を付けた機器の鍵が読み込まれていない")
        self.assertEqual(
            [], [m for m in messages if "読めない行" in m],
            "正しい行を「読めない行」と呼んでいる: %r" % (messages,))

    def test_non_ascii_comment_line_does_not_block_connections(self):
        """コメント行（#）の日本語でも接続が止まらないこと。"""
        self._write_known_hosts(
            ["# 検証用ﾙｰﾀ ★重要★", self._line("192.0.2.8", self.key_a)])

        client, _messages = self._setup("192.0.2.5")

        self.assertIsNotNone(
            client.get_host_keys().get("192.0.2.8"),
            "コメント行の日本語で全機器が中止になっている")

    def test_tofu_save_keeps_the_annotated_key(self):
        """注釈つきファイルでも、初回接続の鍵が保存でき、元の鍵も残ること。"""
        import paramiko
        from core.ssh_connection import _TofuHostKeyPolicy
        self._write_known_hosts(
            [self._line("192.0.2.8", self.key_a, "ラボ")])

        client, _messages = self._setup("192.0.2.5")
        policy = _TofuHostKeyPolicy(self.path)
        save_errors = []
        policy._on_save_error = save_errors.append
        policy.missing_host_key(client, "192.0.2.5", self.key_b)

        saved = self.path.read_bytes()
        self.assertEqual([], save_errors,
                         "保存に失敗している: %r" % (save_errors,))
        self.assertIn(self.key_b.get_base64().encode("ascii"), saved,
                      "初回接続の鍵が保存されていない")
        self.assertIn(self.key_a.get_base64().encode("ascii"), saved,
                      "注釈を付けた機器の鍵が保存で消えている")

    def test_unreadable_file_still_refuses_every_host(self):
        """ファイルそのものを開けないときは、今までどおり全機器を中止すること。"""
        from core.ssh_connection import HostKeyStoreError
        self._write_known_hosts(
            [self._line("192.0.2.8", self.key_a, "ラボ")])

        with mock.patch("core.ssh_connection.load_known_hosts",
                        side_effect=PermissionError("使用中")):
            with self.assertRaises(HostKeyStoreError):
                self._setup("192.0.2.5")


if __name__ == "__main__":
    unittest.main()
