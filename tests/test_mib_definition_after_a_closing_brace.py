r"""前の定義を閉じる `}` と同じ行から始まる定義にも名前が付くことを検証する。

実 MIB（Cisco 配布の DS3-MIB.my）は、前の定義を閉じる `}` と同じ行に
次の定義の名前を書く:
    ds3Groups      OBJECT IDENTIFIER ::= {
    ds3Conformance 1 } ds3Compliances OBJECT
    IDENTIFIER ::= { ds3Conformance 2 }
ASN.1 は空白と改行を区別しないので、珍しいが正しい書き方。

実測（1.3.1）: 1.3.1 で定義の名前を行頭に錨で留めた（b603dfb。語の途中や
`FROM SNMPv2-TC` から始め直して偽の名前を付けるのを防ぐため）ことで、
この名前が抽出にも宣言名にも入らなくなった。DS3-MIB では
name_to_oid['ds3Compliances'] が None、resolve_oid('1.3.6.1.2.1.10.30.14.2')
が 'ds3Conformance.2'（v1.3.0 では 'ds3Compliances'）。同じ書き方の MIB では
配下も解決しない（合成 MIB で exCompliances も子の exLeaf も None）。

直し方: 行頭の錨はそのまま残し、区間の本文で「`}` の直後（同じ行）に
定義の名前と型キーワードが続く」所だけ、`}` の後ろに改行を差し込んで
から抽出する。起点は `}` の位置だけなので、b603dfb が塞いだ経路（語の
途中や `FROM <モジュール名>` からの始め直し）は開かず、抽出の速さも
変わらない。抽出の規則が変わるので、1.3.1 のキャッシュを使い続けない
よう MIB_PARSER_VERSION は 1.3.2 で上げてある。
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

NL = "\r\n"


def brace_mib(word, keyword_lines=("OBJECT", "IDENTIFIER ::= { exConformance 2 }")):
    return NL.join([
        "EXAMPLE-DS-MIB DEFINITIONS ::= BEGIN",
        "exRoot OBJECT IDENTIFIER ::= { enterprises 65001 }",
        "exConformance OBJECT IDENTIFIER ::= { exRoot 14 }",
        "exGroups      OBJECT IDENTIFIER ::= {",
        "exConformance 1 } " + word + " " + keyword_lines[0],
        *keyword_lines[1:],
        "exLeaf OBJECT IDENTIFIER ::= { exCompliances 1 }",
        "END",
        "",
    ])


class MibDefinitionAfterAClosingBraceTest(unittest.TestCase):
    def _resolver(self, text):
        """exe の隣に mibs/ を作り、text を置いて読み込む

        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-brace-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(exe_dir, "mibs"))
        with open(os.path.join(exe_dir, "mibs", "T.my"), "wb") as f:
            f.write(text.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        with contextlib.redirect_stdout(io.StringIO()):
            return MIBResolver()

    def test_a_definition_after_a_closing_brace_keeps_its_name(self):
        """`}` と同じ行から始まる定義にも名前が付き、配下も解決すること。"""
        r = self._resolver(brace_mib("exCompliances"))
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.65001.14.2"),
                         "exCompliances")
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.65001.14.2.1"), "exLeaf")
        # 前の定義（`}` で閉じた側）もこれまでどおり
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.65001.14.1"), "exGroups")

    def test_every_extracted_kind_after_a_closing_brace_keeps_its_name(self):
        """型キーワードの種類によらず、`}` の後ろの定義に名前が付くこと。"""
        for label, lines in (
                ("OBJECT-TYPE", ("OBJECT-TYPE", "    SYNTAX Integer32",
                                 "    STATUS current",
                                 "    ::= { exConformance 2 }")),
                ("OBJECT-IDENTITY", ("OBJECT-IDENTITY", "    STATUS current",
                                     "    ::= { exConformance 2 }")),
                ("NOTIFICATION-TYPE", ("NOTIFICATION-TYPE",
                                       "    STATUS current",
                                       "    ::= { exConformance 2 }")),
                ("TRAP-TYPE", ("TRAP-TYPE", "    ENTERPRISE exConformance",
                               "    ::= 2"))):
            with self.subTest(kind=label):
                r = self._resolver(brace_mib("exCompliances", lines))
                oid = ("1.3.6.1.4.1.65001.14.0.2" if label == "TRAP-TYPE"
                       else "1.3.6.1.4.1.65001.14.2")
                self.assertEqual(r.resolve_oid(oid), "exCompliances")
                self.assertEqual(r.resolve_oid("1.3.6.1.4.1.65001.14.1"),
                                 "exGroups")

    def test_an_uppercase_word_after_a_closing_brace_is_not_a_name(self):
        """`}` の後ろでも、全部大文字の語やその切れ端を名前にしないこと。"""
        for word in ("PDU-1", "SNMP-TARGET", "IEEE8021-PAE"):
            with self.subTest(word=word):
                r = self._resolver(brace_mib(word))
                self.assertEqual(r.resolve_oid("1.3.6.1.4.1.65001.14.2"),
                                 "exConformance.2")
                for name in (word, word[word.index("-"):],
                             word[1:], "8021-PAE"):
                    self.assertIsNone(r.resolve_name(name))

    def test_a_brace_inside_a_definition_does_not_split_it(self):
        """定義の途中の `}`（SYNTAX の列挙など）の後ろの節キーワードで切らないこと。"""
        text = NL.join([
            "EXAMPLE-ST-MIB DEFINITIONS ::= BEGIN",
            "exRoot OBJECT IDENTIFIER ::= { enterprises 65002 }",
            "exState OBJECT-TYPE",
            "    SYNTAX INTEGER { up(1), down(2) } MAX-ACCESS read-only",
            "    STATUS current",
            "    ::= { exRoot 3 }",
            "END",
            "",
        ])
        r = self._resolver(text)
        self.assertEqual(r.resolve_name("exState"), "1.3.6.1.4.1.65002.3")


if __name__ == "__main__":
    unittest.main()
