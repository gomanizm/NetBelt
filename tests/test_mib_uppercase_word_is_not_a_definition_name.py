r"""全部大文字の語が定義の名前として登録されないことを検証する。

MIB の定義は `名前 <型キーワード> ... ::= { 親 添字 }` を正規表現で拾う。
起点の `([\w-]+)` は語なら何でも通るので、IMPORTS 節の直後に根の定義が
あり、名前と型キーワードが離れていると、`IMPORTS` から始まった一致が
その根の `::=` まで伸びて `IMPORTS` を定義の名前として登録していた。

実測（IMPORTS 節の直後に `myMib MODULE-IDENTITY ... ::= { enterprises
99999 }` を置いた MIB。名前と型キーワードの間を 4 通りに変える）:
- 同一行 → myMib=1.3.6.1.4.1.99999（正）
- 改行 1 つ → myMib=1.3.6.1.4.1.99999（正）
- 空行を 1 つ挟む → 定義は [('myLeaf','myMib','1'),
  ('IMPORTS','enterprises','99999')] になり、myMib も myLeaf も None。
  resolve_oid('1.3.6.1.4.1.99999') が 'IMPORTS' になる
- 名前の次の行がまるごとコメント → 空行のときと同じ（コメントは空白に
  なるが改行は残るので、名前と型キーワードの間の改行が 2 つになる）

本体の根が解決できないので、その配下の Trap もまとめて名前が付かず、
そのうえ企業 OID に `IMPORTS` という嘘の名前が出る。

直し方: 名前と型キーワードの間の許容を広げ続けるのではなく、抽出の
開始側に「全部大文字の語は定義の名前として認めない」規則を掛ける。
ASN.1 の値参照は小文字で始まるので、IMPORTS / EXPORTS のような節の
キーワードが定義の名前になること自体が誤り。折り返しの形に依存しなく
なるので、境界の許容を増やす必要も無くなる。抽出の規則が変わるので
MIB_PARSER_VERSION を上げた。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

NL = "\r\n"

# IMPORTS 節の直後に根を置いた MIB。%s に根の見出し（名前と型キーワード）
BASE = NL.join([
    "TEST-MIB DEFINITIONS ::= BEGIN",
    "IMPORTS",
    "    MODULE-IDENTITY, OBJECT-TYPE",
    "        FROM SNMPv2-SMI;",
    "%s",
    "    LAST-UPDATED \"202601010000Z\"",
    "    ORGANIZATION \"example.com\"",
    "    ::= { enterprises 99999 }",
    "myLeaf OBJECT IDENTIFIER ::= { myMib 1 }",
    "END",
    "",
])

# 名前と型キーワードの間の形。どれも実 MIB にある書き方
SHAPES = {
    "same_line": "myMib MODULE-IDENTITY",
    "one_newline": "myMib" + NL + "    MODULE-IDENTITY",
    "blank_line": "myMib" + NL + NL + "    MODULE-IDENTITY",
    "comment_line": ("myMib" + NL + "    -- the module root" + NL
                     + "    MODULE-IDENTITY"),
    "trailing_comment": ("myMib -- the module root" + NL
                         + "    MODULE-IDENTITY"),
}


class MibUppercaseWordIsNotADefinitionNameTest(unittest.TestCase):
    def _resolver(self, text):
        """exe の隣の mibs/ へ T.my を置いて読み込む

        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-upper-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(exe_dir, "mibs"))
        path = os.path.join(exe_dir, "mibs", "T.my")
        with open(path, "wb") as f:
            f.write(text.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        return MIBResolver(), path

    def test_the_root_resolves_however_the_header_is_wrapped(self):
        """名前と型キーワードがどう離れていても、根が解決できること。"""
        for shape, head in SHAPES.items():
            with self.subTest(shape=shape):
                resolver, _ = self._resolver(BASE % head)
                self.assertEqual(resolver.resolve_name("myMib"),
                                 "1.3.6.1.4.1.99999",
                                 "根が解決できていない")
                self.assertEqual(resolver.resolve_name("myLeaf"),
                                 "1.3.6.1.4.1.99999.1",
                                 "根の配下が解決できていない")

    def test_imports_never_becomes_a_name(self):
        """企業 OID に `IMPORTS` という名前が付かないこと。"""
        for shape, head in SHAPES.items():
            with self.subTest(shape=shape):
                resolver, path = self._resolver(BASE % head)
                names = [d[0] for d
                         in resolver._extract_mib_definitions(path)]
                self.assertNotIn("IMPORTS", names,
                                 "節のキーワードが定義の名前になっている: "
                                 "%r" % (names,))
                self.assertNotEqual(
                    resolver.resolve_oid("1.3.6.1.4.1.99999"), "IMPORTS",
                    "企業 OID に IMPORTS という名前が付いている")
                self.assertIsNone(resolver.resolve_name("IMPORTS"))

    def test_a_lowercase_name_is_still_a_definition(self):
        """小文字で始まる名前は、これまでどおり定義として拾うこと（対照）。"""
        resolver, path = self._resolver(BASE % SHAPES["same_line"])
        names = [d[0] for d in resolver._extract_mib_definitions(path)]
        self.assertIn("myMib", names)
        self.assertIn("myLeaf", names)

    def test_a_name_with_uppercase_after_the_first_letter_still_works(self):
        """途中に大文字がある名前（実 MIB の普通の形）が落ちないこと。"""
        text = (
            "CASE-MIB DEFINITIONS ::= BEGIN" + NL
            + "cRoot OBJECT IDENTIFIER ::= { enterprises 88888 }" + NL
            + "cHTTPStatus OBJECT IDENTIFIER ::= { cRoot 7 }" + NL
            + "END" + NL)
        resolver, _ = self._resolver(text)
        self.assertEqual(resolver.resolve_name("cHTTPStatus"),
                         "1.3.6.1.4.1.88888.7")


if __name__ == "__main__":
    unittest.main()
