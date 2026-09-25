"""読めない行の警告が「この機器とは無関係」と断定しないことを検証する。

何が起きていたか（基準 f4cad23 / aa38a2b で実測）。known_hosts の
「読めない行」は _refuse_or_warn_broken_lines() が名前欄を見て振り分ける
が、その照合（_hostnames_match）は完全一致とハッシュ化名だけで、
OpenSSH のワイルドカードを解釈しない。そのため

    @revoked * ecdsa-sha2-nistp256 <有効な鍵>

を 1 行だけ置いて 127.0.0.1 へ繋ぐと、

    読めない行? : True
    名前欄       : ['*']
    _hostnames_match(['*'], '127.0.0.1') : False
    → 中止せず、次の警告だけを出して接続を続ける
      「[NetBelt] 警告: known_hosts に読めない行があります
        （この機器の接続先を指す行ではないので、接続は続けます）」
    paramiko lookup('127.0.0.1') : None  ← 既知の鍵が無い＝TOFU が受け入れる

同じ行を `@revoked 127.0.0.1 ...`（完全一致）に変えると中止するので、
違いはワイルドカードだけ。`@cert-authority *.example.com ...` へ
sw1.example.com で繋ぐ場合も同じで、SSH CA を使う組織の known_hosts は
この行をそのまま持っているのが普通なので、その環境では全機器が恒常的に
TOFU になる。にもかかわらず警告は「この機器の接続先を指す行ではない」と
断定しており、利用者を逆向きに安心させていた。

利用者の決定（2026-09-23）: 文言だけ直す。止めない。警告から
「この機器の接続先を指す行ではないので」という断定を外し、
「ワイルドカードを含む読めない行があり、この機器を指している可能性が
あります」の趣旨へ変える（パターン照合は実装しない）。

どう直したか。_refuse_or_warn_broken_lines() の警告文から断定を落とし、
「名前欄が完全には一致しなかった」という事実と、「ワイルドカードを含む
行はこの機器を指している可能性があり、その場合は初回接続の扱いに戻る」
という注意を載せた。振り分けの実装（完全一致とハッシュ化名だけを見る）は
決定どおり変えていない。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

CLAIM = "この機器の接続先を指す行ではない"


class KnownHostsWildcardWarningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.key = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khwildcard-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "known_hosts"

    def _write_marked(self, marker, names):
        """OpenSSH の印つきの行（paramiko からは読めない行）を 1 行書く。"""
        line = "%s %s %s %s" % (marker, names, self.key.get_name(),
                                self.key.get_base64())
        self.path.write_text(line + "\n", encoding="utf-8")
        return line

    def _warn(self, host):
        """_setup_host_keys を通し、画面に出た警告をつないで返す。"""
        import paramiko
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host=host, port=22, username="admin")
        self.addCleanup(conn.deleteLater)
        messages = []
        conn.output_received.connect(messages.append)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return "".join(messages)

    def test_the_warning_does_not_claim_the_line_is_unrelated(self):
        """ワイルドカード行なのに「この機器を指す行ではない」と断定しないこと。"""
        self._write_marked("@revoked", "*")

        warning = self._warn("192.0.2.9")

        self.assertNotIn(CLAIM, warning,
                         "照合していないのに無関係だと断定している: %r"
                         % (warning,))

    def test_the_warning_says_a_wildcard_line_may_cover_this_host(self):
        """この機器を指している可能性があることを伝えること。"""
        self._write_marked("@cert-authority", "*.example.com")

        warning = self._warn("sw1.example.com")

        self.assertIn("ワイルドカード", warning,
                      "ワイルドカードへの言及が無い: %r" % (warning,))
        self.assertIn("可能性", warning,
                      "この機器を指しうることが伝わらない: %r" % (warning,))

    def test_a_wildcard_line_still_does_not_stop_the_connection(self):
        """決定どおり、ワイルドカード行では接続を止めないこと（対照）。"""
        self._write_marked("@cert-authority", "*.example.com")

        # 例外にならないこと自体が確認事項
        warning = self._warn("sw1.example.com")

        self.assertIn("警告", warning, "警告が出ていない: %r" % (warning,))

    def test_the_warning_still_names_the_line_and_the_file(self):
        """行番号・行の内容・ファイル名は今までどおり出すこと（対照）。"""
        line = self._write_marked("@revoked", "*")

        warning = self._warn("192.0.2.9")

        self.assertIn("1 行目", warning, "行番号が出ていない: %r" % (warning,))
        self.assertIn(line, warning, "行の内容が出ていない: %r" % (warning,))
        self.assertIn(str(self.path), warning,
                      "ファイル名が出ていない: %r" % (warning,))

    def test_an_exactly_matching_marked_line_still_refuses(self):
        """完全一致の印つき行は、今までどおり接続を中止すること（対照）。"""
        from core.ssh_connection import HostKeyStoreError
        self._write_marked("@revoked", "192.0.2.10")

        with self.assertRaises(HostKeyStoreError):
            self._warn("192.0.2.10")


if __name__ == "__main__":
    unittest.main()
