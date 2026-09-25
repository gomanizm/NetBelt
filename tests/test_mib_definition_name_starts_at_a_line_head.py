r"""定義の名前が行頭から始まる語であることを検証する。

MIB の定義は `名前 <型キーワード> ... ::= { 親 添字 }` を正規表現で拾う。
前の周で「全部大文字の語は定義の名前として認めない」を
`(?![A-Z][A-Z0-9-]*\b)([\w-]+)` で足したが、起点に左の境界が無い。
正規表現は 1 文字ずつ位置をずらして試すので、全部大文字の語を弾いた
あとにその語の途中から始め直し、そこから先を名前として登録する。

実測 A（`<語> OBJECT IDENTIFIER ::= { acmeRoot 2 }` の 1 行に当てた結果）:
  PDU-1 → '-1' / SNMP-TARGET → '-TARGET' / IEEE8021-PAE → '8021-PAE'
実害も出る。`acmeRoot ::= { enterprises 65001 }` と
`PDU-1 ::= { acmeRoot 2 }` を置くと、親の acmeRoot は解決できるので
resolve_oid('1.3.6.1.4.1.65001.2') が '-1' になる。この修正が狙って
潰したはずの「企業 OID に嘘の名前」を、この修正自身が作っていた。

実測 B（偽名の経路は全部大文字の語に限らない）: 名前と型キーワードの
間は `\s+`（改行いくつでも可）なので、IMPORTS 節の `FROM <モジュール名>`
のモジュール名が全部大文字でなければ（SNMPv2-TC / SNMPv2-SMI など実 MIB
でいちばん多い並び）そのまま名前になる。根の名前と型キーワードの間に
空行か独立したコメント行がある MIB で、
resolve_oid('1.3.6.1.4.1.99999') が 'SNMPv2-TC'、根の myMib は None。

直し方: 大文字の規則に頼らず、定義の名前を行頭に錨で留める
（`^[ \t]*` + 全部大文字を弾く先読み）。語の途中から始め直せなくなるので
A は定義ごと落ち（嘘の名前を付けない）、B は行頭でない `FROM SNMPv2-TC`
が起点になれず根が正しく解決する。あわせて _MIB_LOCAL_NAME の
re.findall に re.MULTILINE を渡す（`^` が死んでいると宣言を拾えない）。
抽出の規則が変わるので MIB_PARSER_VERSION を上げた。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

NL = "\r\n"

# (A) 全部大文字の語で宣言された節。親の acmeRoot は解決できる
UPPER_WORDS = ("PDU-1", "SNMP-TARGET", "IEEE8021-PAE")


def upper_mib(word):
    return NL.join([
        "HY-MIB DEFINITIONS ::= BEGIN",
        "acmeRoot OBJECT IDENTIFIER ::= { enterprises 65001 }",
        word + " OBJECT IDENTIFIER ::= { acmeRoot 2 }",
        "END",
        "",
    ])


# (B) IMPORTS 節のモジュール名が偽名になる形。`FROM SNMPv2-SMI;` の次が
# 根の myMib で、その名前と型キーワードの間が離れている
def imports_mib(gap):
    return NL.join([
        "MY-MIB DEFINITIONS ::= BEGIN",
        "IMPORTS",
        "    MODULE-IDENTITY, NOTIFICATION-TYPE",
        "        FROM SNMPv2-TC",
        "    OBJECT-TYPE",
        "        FROM SNMPv2-SMI;",
        "myMib" + gap + "OBJECT IDENTIFIER ::= { enterprises 99999 }",
        "myLeaf OBJECT IDENTIFIER ::= { myMib 1 }",
        "END",
        "",
    ])


# 名前と型キーワードの間の形。どれも実 MIB にある書き方
GAPS = {
    "same_line": " ",
    "one_newline": NL + "    ",
    "blank_line": NL + NL + "    ",
    "comment_line": NL + "    -- the company root" + NL + "    ",
}


class MibDefinitionNameStartsAtALineHeadTest(unittest.TestCase):
    def _resolver(self, text, name="T.my"):
        """exe の隣の mibs/ へ 1 ファイル置いて読み込む

        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-anchor-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(exe_dir, "mibs"))
        path = os.path.join(exe_dir, "mibs", name)
        with open(path, "wb") as f:
            f.write(text.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        return MIBResolver(), path

    def test_no_name_is_taken_from_the_middle_of_an_uppercase_word(self):
        """全部大文字の語の途中から、名前を切り出さないこと。"""
        for word in UPPER_WORDS:
            with self.subTest(word=word):
                resolver, path = self._resolver(upper_mib(word))
                names = [d[0] for d
                         in resolver._extract_mib_definitions(path)]
                self.assertEqual(
                    names, ["acmeRoot"],
                    "大文字の語の途中から名前を切り出している: %r" % (names,))

    def test_the_enterprise_oid_keeps_no_false_name(self):
        """企業 OID に、語の切れ端の嘘の名前が付かないこと。"""
        for word in UPPER_WORDS:
            with self.subTest(word=word):
                resolver, _ = self._resolver(upper_mib(word))
                self.assertEqual(
                    resolver.resolve_oid("1.3.6.1.4.1.65001.2"),
                    "acmeRoot.2",
                    "企業 OID に嘘の名前が付いている")

    def test_an_imported_module_name_never_becomes_a_definition_name(self):
        """IMPORTS の `FROM <モジュール名>` が定義の名前にならないこと。"""
        for shape, gap in GAPS.items():
            with self.subTest(shape=shape):
                resolver, path = self._resolver(imports_mib(gap), "MY.my")
                names = [d[0] for d
                         in resolver._extract_mib_definitions(path)]
                self.assertNotIn(
                    "SNMPv2-TC", names,
                    "IMPORTS のモジュール名が定義の名前になっている: "
                    "%r" % (names,))
                self.assertNotIn("SNMPv2-SMI", names)

    def test_the_root_resolves_however_the_header_is_wrapped(self):
        """根と配下が、名前と型キーワードの離れ方によらず解決すること。"""
        for shape, gap in GAPS.items():
            with self.subTest(shape=shape):
                resolver, _ = self._resolver(imports_mib(gap), "MY.my")
                self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.99999"),
                                 "myMib", "企業 OID に嘘の名前が付いている")
                self.assertEqual(resolver.resolve_name("myMib"),
                                 "1.3.6.1.4.1.99999",
                                 "根が解決できていない")
                self.assertEqual(resolver.resolve_name("myLeaf"),
                                 "1.3.6.1.4.1.99999.1",
                                 "根の配下が解決できていない")

    def test_an_indented_definition_is_still_extracted(self):
        """字下げした定義は、これまでどおり拾うこと（対照）。"""
        text = NL.join([
            "IND-MIB DEFINITIONS ::= BEGIN",
            "    indRoot OBJECT IDENTIFIER ::= { enterprises 77777 }",
            "\tindLeaf OBJECT IDENTIFIER ::= { indRoot 3 }",
            "END",
            "",
        ])
        resolver, _ = self._resolver(text, "IND.my")
        self.assertEqual(resolver.resolve_name("indRoot"),
                         "1.3.6.1.4.1.77777")
        self.assertEqual(resolver.resolve_name("indLeaf"),
                         "1.3.6.1.4.1.77777.3")


if __name__ == "__main__":
    unittest.main()
