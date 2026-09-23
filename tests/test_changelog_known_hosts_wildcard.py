"""リリースノートが、ワイルドカードの行について利用者を安心させないことを検証する。

何が起きていたか（基準 097550c で実測）。CHANGELOG.md の [Unreleased]
（ZIP には CHANGELOG.txt として入る）が

    `known_hosts` に読めない行があるときは、その行が指す機器への接続だけを断ります

と書いていた。ところが読めない行の振り分け（_hostnames_match）は完全一致と
ハッシュ化名しか見ないので、`@revoked *` の行で 192.0.2.9 へ繋いでも、
`@cert-authority *.example.com` の行で sw1.example.com へ繋いでも、
接続は続き、既知の鍵も無い（TOFU が受け入れる）。ワイルドカードの行が
指す機器への接続は断っていない。

利用者の決定（2026-09-23）: 止めない。パターン照合は実装せず、警告の
文言だけを直す（c840cda で警告文は直した）。リリースノートにだけ
「その行が指す機器への接続を断る」という言い過ぎが残り、警告文と同じ
向きで利用者を安心させていた。

どう直したか。CHANGELOG.md の該当行を、断るのは名前欄が接続先と完全に
一致するときだけで、ワイルドカードの行は照合せず警告して接続することが
分かる書き方に直した。コードは触っていない。
"""
import io
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _known_hosts_unreadable_line_bullets():
    """CHANGELOG.md 全体から、known_hosts の読めない行を述べる項目を返す。

    節の見出しでは探さない。リリースの切り出しで [Unreleased] は
    [1.3.1] などへ書き換わり、その上に空の [Unreleased] が足されることも
    ある。見出しに頼ると、リリースのワークフロー（タグで pytest を流す）が
    このテストで止まってしまう。
    """
    with io.open(os.path.join(REPO_ROOT, "CHANGELOG.md"),
                 encoding="utf-8") as f:
        text = f.read()
    return [line for line in text.splitlines()
            if "known_hosts" in line and "読めない行" in line]


class ChangelogKnownHostsWildcardTest(unittest.TestCase):
    def setUp(self):
        self.bullets = _known_hosts_unreadable_line_bullets()
        self.assertEqual(len(self.bullets), 1,
                         "known_hosts の読めない行を述べる項目が 1 つでない: %r"
                         % (self.bullets,))
        self.bullet = self.bullets[0]

    def test_does_not_claim_every_covered_host_is_refused(self):
        """「その行が指す機器への接続だけを断ります」と言い切らないこと。"""
        self.assertNotIn("その行が指す機器への接続", self.bullet)

    def test_says_only_an_exact_name_match_is_refused(self):
        """断るのは名前欄が完全に一致するときだけだと書いてあること。"""
        self.assertIn("完全に一致", self.bullet)

    def test_says_wildcard_lines_only_warn(self):
        """ワイルドカードの行は照合せず、警告して接続すると書いてあること。"""
        self.assertIn("ワイルドカード", self.bullet)
        self.assertIn("警告", self.bullet)

    def test_still_mentions_the_key_conflict_refusal(self):
        """保存済みと違う鍵で上書きせず中止する旨は、これまでどおり残ること。"""
        self.assertIn("上書きせず中止", self.bullet)


if __name__ == "__main__":
    unittest.main()
