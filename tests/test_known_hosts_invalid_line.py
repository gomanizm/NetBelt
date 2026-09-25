"""known_hosts の「読めない行」のうち、paramiko が例外を投げる種類を検証する。

実測（334cd72 / paramiko 4.0.0）: paramiko.hostkeys.InvalidHostKey は
SSHException を継承しておらず素の Exception である
（issubclass(paramiko.hostkeys.InvalidHostKey, paramiko.SSHException) → False）。
HostKeyEntry.from_line は 3 つ目の欄を先に base64 デコードするので、
鍵欄が壊れている行と、OpenSSH の正規の書式である @cert-authority /
@revoked で始まる行（印を剥がさないため 3 つ目の欄が鍵種別の文字列に
なる）は InvalidHostKey を投げる。そのため HostKeys.load の
`except SSHException: continue` も unreadable_known_hosts_lines() の
`except paramiko.SSHException` も拾えず、client.load_host_keys() の時点で
例外になって _setup_host_keys の `except Exception` に落ちていた。

検査役の実測（行の種類ごとに、その行が指す 192.0.2.9 宛と、無関係な
192.0.2.8 宛を両方通した表）:
  フィールド不足      → 192.0.2.9: 行を名指しして中止 / 192.0.2.8: 続行（警告 1 件）
  未知の鍵種別 sk-*   → 同上
  証明書 *-cert-v01   → 同上
  base64 が壊れている → 192.0.2.9: 全機器中止 / 192.0.2.8: 全機器中止
  @cert-authority 行  → 同上
  @revoked 行         → 同上
後半 3 種では行番号も行の内容も出ず、関係のない機器まで繋がらなくなる。
そのとき出る文は「ファイルを開けない（権限・排他・入出力エラー）状態です」で、
実際の原因（壊れた行）を指していない。

利用者の決定（2026-09-20）: その接続先だけ断る。known_hosts に読めない行が
あったら、その行を名指しで知らせ、その行が指す接続先への接続だけを断る
（他の機器への接続は今までどおり）。壊れた行を黙って消さない。

直し方: 行ごとに握りつぶす自前のローダ（load_known_hosts）を通し、
読めた行だけを client の HostKeys へ入れる。読めない行の収集は
load より先に行い、例外の種類は Exception まで広げる。@cert-authority /
@revoked の印は名前欄より前にあるので、どの接続先を指す行かを見るときは
印を 1 つ読み飛ばす。ファイル全体を読めなかったときの案内は、開けない場合と
読み取れない場合の両方を書く。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class KnownHostsInvalidLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()
        cls.key_c = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khinvalid-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "known_hosts"

    def _line(self, hostname, key):
        return "%s %s %s" % (hostname, key.get_name(), key.get_base64())

    def _truncated_line(self, hostname, key):
        """鍵欄が base64 として壊れている行（末尾 1 文字欠け）"""
        return "%s %s %s" % (hostname, key.get_name(), key.get_base64()[:-1])

    def _marked_line(self, marker, hostname, key):
        """OpenSSH の印つきの行（@cert-authority / @revoked）"""
        return "%s %s" % (marker, self._line(hostname, key))

    def _write_known_hosts(self, lines):
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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

    def _assert_refused_naming(self, host, lineno, broken_line):
        from core.ssh_connection import HostKeyStoreError
        with self.assertRaises(HostKeyStoreError) as caught:
            self._setup(host)
        message = str(caught.exception)
        self.assertIn("%d 行目" % lineno, message,
                      "何行目かが分からない: %s" % message)
        self.assertIn(broken_line, message,
                      "どの行かが分からない: %s" % message)
        return message

    def _assert_other_host_connects(self, broken_line):
        client, messages = self._setup("192.0.2.8")
        self.assertIsNotNone(
            client.get_host_keys().get("192.0.2.8"),
            "読めている行の鍵まで失われている（関係のない機器が繋がらない）")
        self.assertTrue(any(broken_line in m for m in messages),
                        "読めない行があることを知らせていない: %r" % (messages,))

    def test_a_broken_base64_line_refuses_only_its_own_host(self):
        """鍵欄が base64 として壊れた行は、その接続先だけを名指しで断ること。"""
        broken = self._truncated_line("192.0.2.9", self.key_c)
        self._write_known_hosts([self._line("192.0.2.8", self.key_a), broken])

        self._assert_refused_naming("192.0.2.9", 2, broken)

    def test_a_broken_base64_line_does_not_block_other_hosts(self):
        """鍵欄が壊れた行があっても、他の機器は今までどおり繋がること。"""
        broken = self._truncated_line("192.0.2.9", self.key_c)
        self._write_known_hosts([self._line("192.0.2.8", self.key_a), broken])

        self._assert_other_host_connects(broken)

    def test_a_cert_authority_line_refuses_only_its_own_host(self):
        """@cert-authority 行は、印の次の名前欄が指す接続先を断ること。"""
        broken = self._marked_line("@cert-authority", "192.0.2.9", self.key_c)
        self._write_known_hosts([self._line("192.0.2.8", self.key_a), broken])

        self._assert_refused_naming("192.0.2.9", 2, broken)

    def test_a_cert_authority_line_does_not_block_other_hosts(self):
        """@cert-authority 行があっても、他の機器は繋がること。"""
        broken = self._marked_line("@cert-authority", "192.0.2.9", self.key_c)
        self._write_known_hosts([self._line("192.0.2.8", self.key_a), broken])

        self._assert_other_host_connects(broken)

    def test_a_revoked_line_refuses_only_its_own_host(self):
        """@revoked 行も同じく、その接続先だけを断ること。"""
        broken = self._marked_line("@revoked", "192.0.2.9", self.key_c)
        self._write_known_hosts([self._line("192.0.2.8", self.key_a), broken])

        self._assert_refused_naming("192.0.2.9", 2, broken)

    def test_a_revoked_line_does_not_block_other_hosts(self):
        """@revoked 行があっても、他の機器は繋がること。"""
        broken = self._marked_line("@revoked", "192.0.2.9", self.key_c)
        self._write_known_hosts([self._line("192.0.2.8", self.key_a), broken])

        self._assert_other_host_connects(broken)

    def test_an_invalid_line_survives_a_save(self):
        """例外を投げる種類の読めない行も、保存で消さないこと。"""
        from core.ssh_connection import _TofuHostKeyPolicy
        broken = self._truncated_line("192.0.2.9", self.key_c)
        self._write_known_hosts([self._line("192.0.2.8", self.key_a), broken])
        client, _ = self._setup("192.0.2.8")

        policy = _TofuHostKeyPolicy(self.path)
        saved = []
        policy._on_save_error = saved.append
        policy.missing_host_key(client, "192.0.2.4", self.key_b)

        self.assertEqual(saved, [], "保存に失敗している: %r" % (saved,))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn(broken, text, "読めない行が黙って消えている")
        self.assertIn(self.key_b.get_base64(), text,
                      "新しく受け入れた鍵が保存されていない")
        self.assertIn(self.key_a.get_base64(), text,
                      "元からあった鍵が失われている")

    def test_the_unreadable_file_message_names_both_causes(self):
        """ファイル全体を読めないときの案内が、原因を 1 つに決めつけないこと。"""
        from core.ssh_connection import HostKeyStoreError
        self._write_known_hosts([self._line("192.0.2.8", self.key_a)])

        with mock.patch("core.ssh_connection.unreadable_known_hosts_lines",
                        side_effect=OSError("Permission denied")):
            with self.assertRaises(HostKeyStoreError) as caught:
                self._setup("192.0.2.8")

        message = str(caught.exception)
        self.assertIn("開けない", message, "開けない場合の案内が無い")
        self.assertIn("読み取れない", message, "読み取れない場合の案内が無い")


if __name__ == "__main__":
    unittest.main()
