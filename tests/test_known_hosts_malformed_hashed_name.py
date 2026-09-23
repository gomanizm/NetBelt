"""形の崩れたハッシュ化名（|1|塩|ハッシュ）の行で、全機器の接続が止まらないことを検証する。

何が起きていたか（bc7795f で実測）。known_hosts に

    |1|AAAA|AAAA ecdsa-sha2-nistp256 <有効な鍵>   ← 塩が 20 バイトに復号できない

のような行があると、paramiko の HostKeyEntry.from_line はそのまま読める
行として返す。ところが paramiko が接続先を探す lookup は、ハッシュ化名の
行ごとに HostKeys.hash_host(接続先, 名前) を掛け直し、塩の長さの assert
（または base64 の復号）で例外になる。

- 自前ローダ（load_known_hosts）を通る場合（読めない行・BOM・cp932 で
  読めない注釈のどれかがあるとき）: ローダはこの行を取り込み、
  SSHClient.connect が start_client の前に行う _host_keys.get(...) で
  AssertionError('') になる。利用者に出るのは『接続エラー: 』（本文が空）
  だけで、どの接続先でも同じ。どの行が原因かも分からない。
- paramiko の HostKeys.load を通る場合: 崩れた行より後に行があれば、
  load 内の check() で同じ例外になり「known_hosts を読めないため接続を
  中止しました」（理由は空）で全機器が止まる。崩れた行が最後の行なら
  load は通り、上と同じく connect で『接続エラー: 』（本文が空）になる。

どちらも、1 行の崩れで関係のない機器まで繋がらなくなる。読めない行は
行番号つきで知らせ、名前欄が接続先と一致する行があるときだけ断る、
という既存の扱い（test_known_hosts_broken_line.py）から外れていた。

どう直したか。_iter_known_hosts_lines で、名前欄のハッシュ化名に
hash_host を掛けて例外になる行は、読めない行（entry=None）として返す。
読めない行なので、ローダは取り込まず（lookup が落ちない）、警告で
行番号つきで名指しされ、保存のときにもそのまま書き戻される（消えない）。
形の正しいハッシュ化名の行は、これまでどおり読める行として扱う。

崩れたハッシュ化名の行は、どの機器の行かをこちらで判別できない。
警告は、ワイルドカードの行と同じく「この機器を指している可能性がある」
ことを伝える（完全には一致しなかった、だけでは無関係だと読めてしまう）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402
from paramiko.hostkeys import HostKeys              # noqa: E402

HOST_A = "192.0.2.1"
HOST_B = "192.0.2.2"
# 塩が 20 バイトにならないもの（3 バイト / 空に復号される）と、
# base64 として復号できないもの（binascii.Error）
MALFORMED_NAMES = ["|1|AAAA|AAAA", "|1|!!!!|AAAA", "|1|abc|AAAA"]
# 自前ローダの経路へ回すための、別の読めない行（鍵欄が無い）
BROKEN_LINE = "[192.0.2.9]:2222 ssh-ed25519"


class KnownHostsMalformedHashedNameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="netbelt-khmalformed-"))
        self.addCleanup(shutil.rmtree, str(self.data_dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.data_dir / "known_hosts"

    # --- 土台 ---

    def _line(self, hostname, key):
        return "%s %s %s" % (hostname, key.get_name(), key.get_base64())

    def _write(self, lines):
        self.known_hosts.write_text("".join(line + "\n" for line in lines),
                                    encoding="utf-8")

    def _setup(self, host):
        """本物の SSHClient に _setup_host_keys を通し、画面に出た文を返す。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host, 22, "admin", password="pw")
        self.addCleanup(conn.deleteLater)
        messages = []
        conn.output_received.connect(messages.append)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return client, messages

    def _cases(self):
        """(説明, 崩れた行の行番号, 崩れた行, known_hosts の中身) を返す。"""
        for name in MALFORMED_NAMES:
            malformed = self._line(name, self.key_a)
            plain_a = self._line(HOST_A, self.key_a)
            yield ("paramiko の読み込み経路: %s" % name, 1, malformed,
                   [malformed, plain_a])
            yield ("paramiko の読み込み経路・崩れた行が最後: %s" % name,
                   2, malformed, [plain_a, malformed])
            yield ("自前ローダの経路: %s" % name, 1, malformed,
                   [malformed, plain_a, BROKEN_LINE])

    # --- 崩れた行を読めない行として扱うこと ---

    def test_a_malformed_hashed_name_is_listed_as_unreadable(self):
        """崩れたハッシュ化名の行が、行番号つきで読めない行に入ること。"""
        from core.ssh_connection import unreadable_known_hosts_lines
        for title, lineno, malformed, lines in self._cases():
            with self.subTest(title):
                self._write(lines)

                broken = [(no, text) for no, text, _
                          in unreadable_known_hosts_lines(self.known_hosts)]

                self.assertIn((lineno, malformed), broken,
                              "崩れた行が読めない行に入っていない: %r"
                              % (broken,))

    def test_another_host_still_connects_and_the_line_is_named(self):
        """関係のない接続先の検証は進み、崩れた行を名指しで知らせること。"""
        for title, lineno, malformed, lines in self._cases():
            with self.subTest(title):
                self._write(lines)

                client, messages = self._setup(HOST_A)

                # paramiko の connect が start_client の前に引くのと同じ呼び出し
                found = client.get_host_keys().get(HOST_A)
                self.assertIsNotNone(found, "読めている A の鍵が失われている")
                self.assertTrue(found[self.key_a.get_name()] == self.key_a,
                                "A の検証に使う鍵が変わっている")
                self.assertTrue(any("%d 行目: %s" % (lineno, malformed) in m
                                    for m in messages),
                                "崩れた行を名指しで知らせていない: %r"
                                % (messages,))

    def test_saving_another_host_keeps_the_malformed_line(self):
        """B の初回接続を保存しても、崩れた行は消えず B も保存されること。"""
        for title, _lineno, malformed, lines in self._cases():
            with self.subTest(title):
                self._write(lines)

                client, _messages = self._setup(HOST_B)
                client._policy.missing_host_key(client, HOST_B, self.key_b)

                saved = [line for line in
                         self.known_hosts.read_text(encoding="utf-8").splitlines()
                         if line.strip()]
                self.assertIn(malformed, saved,
                              "崩れた行が保存で消えている: %r" % (saved,))
                self.assertIn(self._line(HOST_B, self.key_b), saved,
                              "B の鍵が保存されていない: %r" % (saved,))

    def test_the_warning_does_not_read_as_unrelated(self):
        """崩れたハッシュ化名の行が、この機器を指しうると警告で伝えること。

        どの機器の行かを判別できないので、「完全には一致しなかった」だけ
        では無関係だと読めてしまう（ワイルドカードの行と同じ扱い）。
        """
        for title, _lineno, _malformed, lines in self._cases():
            with self.subTest(title):
                self._write(lines)

                _client, messages = self._setup(HOST_B)

                warning = "".join(messages)
                self.assertIn("ハッシュ化", warning,
                              "崩れたハッシュ化名の行に触れていない: %r"
                              % (warning,))
                self.assertIn("可能性", warning,
                              "この機器を指しうることを伝えていない: %r"
                              % (warning,))

    # --- 今までどおりであること（対照） ---

    def test_a_well_formed_hashed_line_is_still_read(self):
        """形の正しいハッシュ化名の行は、これまでどおり読める行として使うこと。"""
        from core.ssh_connection import unreadable_known_hosts_lines
        hashed_a = HostKeys.hash_host(HOST_A)
        self._write([self._line(hashed_a, self.key_a)])

        self.assertEqual(unreadable_known_hosts_lines(self.known_hosts), [])
        client, messages = self._setup(HOST_A)
        found = client.get_host_keys().get(HOST_A)
        self.assertIsNotNone(found, "ハッシュ化された A の行が使われていない")
        self.assertEqual(messages, [], "読める行だけなのに何か知らせている")


if __name__ == "__main__":
    unittest.main()
