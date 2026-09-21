"""known_hosts に読めない行があるとき、その接続先だけを断ることを検証する。

実測（81664d2 / paramiko 4.0.0）: HostKeys.load は 1 行ごとに
try/except SSHException: continue で囲まれており、さらに
HostKeyEntry.from_line はフィールド不足・未知の鍵種別で None を返すため、
壊れた行は例外にならず黙って読み飛ばされる。known_hosts に
『[192.0.2.9]:2222 ssh-ed25519』（鍵データ欠落）を置いて
SSHConnection._setup_host_keys を通しても HostKeyStoreError は出ず
（検査役の出力『_setup_host_keys: 例外なし（接続は続行）』）、
client.get_host_keys().get("[192.0.2.9]:2222") は None、画面への警告も 0 件。
その結果 paramiko は missing_host_key へ進み、_TofuHostKeyPolicy が何も
聞かずに提示された鍵を受け入れて保存する（＝既知だった接続先が初回接続に
戻り、鍵が変わっていても検出されない）。さらに、その保存のあと known_hosts を
見ると壊れた行は黙って消えていた。
ssh_connection.py の中止メッセージ「壊れた行が 1 つあるだけでも読めなく
なります」は、paramiko 4.0.0 では起きない事象を案内していた
（この経路に落ちるのは IOError/PermissionError だけ）。

利用者の決定（2026-09-20）: その接続先だけ断る。known_hosts に読めない行が
あったら、その行を名指しで知らせ、その行が指す接続先への接続だけを断る
（他の機器への接続は今までどおり）。壊れた行を黙って消さない。

直し方: _setup_host_keys が paramiko と同じ読み方で known_hosts を 1 行ずつ
見て、読めない行を集める。いま繋ごうとしている接続先を指す行があれば
HostKeyStoreError（行番号と行の中身つき）で中止し、ほかの接続先を指す行は
警告として画面に出すだけで接続は続ける。保存（_save_known_hosts）では
読めない行を書き戻し、paramiko の書き出しで消えないようにする。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

BROKEN_LINE = "[192.0.2.9]:2222 ssh-ed25519"


class KnownHostsBrokenLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khbroken-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "known_hosts"

    def _line(self, hostname, key):
        return "%s %s %s" % (hostname, key.get_name(), key.get_base64())

    def _write_known_hosts(self, lines):
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _connection(self, host, port=22):
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host=host, port=port, username="admin")
        self.addCleanup(conn.deleteLater)
        return conn

    def _setup(self, host, port=22):
        """本物の SSHClient に _setup_host_keys を通し、画面に出た文を返す。"""
        import paramiko
        conn = self._connection(host, port)
        messages = []
        conn.output_received.connect(messages.append)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return client, messages

    def test_the_connection_to_the_broken_line_host_is_refused(self):
        """壊れた行が指す接続先への接続は中止すること。"""
        from core.ssh_connection import HostKeyStoreError
        self._write_known_hosts([
            self._line("192.0.2.8", self.key_a),
            BROKEN_LINE,
        ])

        with self.assertRaises(HostKeyStoreError) as caught:
            self._setup("192.0.2.9", 2222)

        message = str(caught.exception)
        self.assertIn("2", message, "何行目かが分からない")
        self.assertIn(BROKEN_LINE, message, "どの行かが分からない")

    def test_another_host_still_connects(self):
        """壊れた行と関係のない接続先は、今までどおり通すこと。"""
        self._write_known_hosts([
            self._line("192.0.2.8", self.key_a),
            BROKEN_LINE,
        ])

        client, messages = self._setup("192.0.2.8")

        self.assertIsNotNone(client.get_host_keys().get("192.0.2.8"),
                             "読めている行の鍵まで失われている")
        self.assertTrue(any(BROKEN_LINE in m for m in messages),
                        "読めない行があることを知らせていない: %r" % (messages,))

    def test_an_unknown_key_type_line_is_treated_as_broken(self):
        """paramiko が扱えない鍵種別の行も、読めない行として扱うこと。"""
        from core.ssh_connection import HostKeyStoreError
        self._write_known_hosts([
            "192.0.2.7 sk-ssh-ed25519@openssh.com AAAABBBB",
        ])

        with self.assertRaises(HostKeyStoreError):
            self._setup("192.0.2.7")

    def test_a_hashed_broken_line_matches_its_host(self):
        """ハッシュ化された名前の壊れた行も、その接続先を断ること。"""
        import paramiko
        from core.ssh_connection import HostKeyStoreError
        hashed = paramiko.hostkeys.HostKeys.hash_host("192.0.2.6")
        self._write_known_hosts(["%s ssh-ed25519" % hashed])

        with self.assertRaises(HostKeyStoreError):
            self._setup("192.0.2.6")

    def test_a_clean_known_hosts_is_unchanged(self):
        """読める行だけなら、今までどおり何も知らせず通すこと。"""
        self._write_known_hosts([
            self._line("192.0.2.8", self.key_a),
            "# comment",
            "",
        ])

        client, messages = self._setup("192.0.2.8")

        self.assertIsNotNone(client.get_host_keys().get("192.0.2.8"))
        self.assertEqual(messages, [], "正常な known_hosts で警告が出ている")

    def test_a_missing_known_hosts_is_unchanged(self):
        """known_hosts がまだ無いときは、今までどおり初回接続として通すこと。"""
        client, messages = self._setup("192.0.2.8")

        self.assertIsNone(client.get_host_keys().get("192.0.2.8"))
        self.assertEqual(messages, [])

    def test_the_broken_line_survives_a_save(self):
        """初回接続の鍵を保存しても、読めない行を消さないこと。"""
        import paramiko
        from core.ssh_connection import _TofuHostKeyPolicy
        self._write_known_hosts([
            self._line("192.0.2.8", self.key_a),
            BROKEN_LINE,
        ])
        client, _ = self._setup("192.0.2.8")

        policy = _TofuHostKeyPolicy(self.path)
        policy.missing_host_key(client, "192.0.2.4", self.key_b)

        text = self.path.read_text(encoding="utf-8")
        self.assertIn(BROKEN_LINE, text, "読めない行が黙って消えている")
        self.assertIn(self.key_b.get_base64(), text,
                      "新しく受け入れた鍵が保存されていない")
        self.assertIn(self.key_a.get_base64(), text,
                      "元からあった鍵が失われている")

    def test_the_refused_host_is_not_re_added_by_tofu(self):
        """断った接続先が、あとから TOFU で初回接続として入り直さないこと。"""
        from core.ssh_connection import HostKeyStoreError
        self._write_known_hosts([BROKEN_LINE])

        with self.assertRaises(HostKeyStoreError):
            self._setup("192.0.2.9", 2222)

        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.strip(), BROKEN_LINE,
                         "断ったのに known_hosts が書き換わっている")


if __name__ == "__main__":
    unittest.main()
