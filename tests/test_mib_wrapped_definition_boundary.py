"""定義の名前と型キーワードが改行で分かれていても、境界として効くことを検証する。

定義の抽出は「名前 + 型キーワード」で始まり（間は `\\s+` なので改行を
許す）、本体は「別の定義が始まる行」を越えないようにしてある。ところが
その境界の判定だけが `[\\w-]+[ \\t]+<型キーワード>` と同じ行に限られて
いた。広さが揃っていないので、名前だけを行に置いた実 MIB の書き方に
当たると境界が見えず、手前の一致が本物の定義を飲み込む。

実測（`IMPORTS` 節で MODULE-IDENTITY を import したあとに、
`myMib` 改行 `    MODULE-IDENTITY` ... `::= { enterprises 99999 }`、
続けて `myLeaf OBJECT IDENTIFIER ::= { myMib 1 }` を置く）:

    === 名前と型キーワードが同じ行（これまで通る形） ===
      extracted: [('myLeaf', 'myMib', '1'), ('myMib', 'enterprises', '99999')]
      resolved : {'1.3.6.1.4.1.99999': 'myMib',
                  '1.3.6.1.4.1.99999.1': 'myLeaf'}
    === 名前だけ別行 ===
      extracted: [('myLeaf', 'myMib', '1'), ('IMPORTS', 'enterprises', '99999')]
      resolved : {'1.3.6.1.4.1.99999': 'IMPORTS'}

`IMPORTS` 改行 `    MODULE-IDENTITY, ...` も「名前 + 型キーワード」に
見えるため、そこから始まった一致が本物の根の `::=` まで伸びる。
モジュールの根が `IMPORTS` という偽の名前で登録され、myMib と myLeaf は
消える。配下の OID は画面に `IMPORTS.1` と出て、mib_cache.json にも
その対応が残る。

被害が出るのは「IMPORTS 節のあと最初に現れる、右辺が `{ 名前 数字 }` の
定義」が改行で割れている場合。ファイル途中の定義を改行で割っても
`::=` を越えられないので害は無い（実測）。

直し方: 境界の判定を、抽出の開始と同じ広さ（名前と型キーワードの間に
改行 1 つを許す）に揃える。全部大文字の行を境界から外す先読みも同じ
広さにする。そうしないと `SYNTAX` 改行 `OBJECT IDENTIFIER` の折り返しで
先読みが外れ、OBJECT-TYPE の本体がそこで打ち切られる。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# IMPORTS 節のあとに根を置いた MIB。%s に見出しの書き方が入る
MODULE_WITH_IMPORTS = (
    "TEST-MIB DEFINITIONS ::= BEGIN\r\n"
    "IMPORTS\r\n"
    "    MODULE-IDENTITY, OBJECT-TYPE\r\n"
    "        FROM SNMPv2-SMI;\r\n"
    "%s"
    "    LAST-UPDATED \"202601010000Z\"\r\n"
    "    ORGANIZATION \"example.com\"\r\n"
    "    ::= { enterprises 99999 }\r\n"
    "myLeaf OBJECT IDENTIFIER ::= { myMib 1 }\r\n"
    "END\r\n")

# 名前と型キーワードの並べ方
ROOT_SHAPES = {
    # 同じ行（これまで通る形）
    "same_line": "myMib MODULE-IDENTITY\r\n",
    # 名前の直後で折る
    "wrapped": "myMib\r\nMODULE-IDENTITY\r\n",
    # 折ったうえに次の行を字下げする
    "wrapped_indent": "myMib\r\n    MODULE-IDENTITY\r\n",
}

# SYNTAX を折り返した OBJECT-TYPE。境界の先読みを広げ忘れると、
# `SYNTAX` 改行 `OBJECT IDENTIFIER` が定義の始まりに見えて本体が切れる
WRAPPED_SYNTAX = (
    "TEST-MIB DEFINITIONS ::= BEGIN\r\n"
    "root OBJECT IDENTIFIER ::= { enterprises 99999 }\r\n"
    "snmpTrap OBJECT IDENTIFIER ::= { root 4 }\r\n"
    "snmpTrapOID OBJECT-TYPE\r\n"
    "    SYNTAX\r\n"
    "        OBJECT IDENTIFIER\r\n"
    "    MAX-ACCESS accessible-for-notify\r\n"
    "    STATUS     current\r\n"
    "    ::= { snmpTrap 1 }\r\n"
    "END\r\n")


class MibWrappedDefinitionBoundaryTest(unittest.TestCase):
    def _resolver(self, text):
        """exe の隣の mibs/ へ T.my を置いて読み込む

        利用者が mibs/ へ MIB を置いて起動する経路をそのまま通す。
        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-wrapped-def-")
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
        resolver = MIBResolver()
        return resolver, path

    def test_imports_does_not_eat_a_root_whose_name_is_on_its_own_line(self):
        """名前を別行に置いた根でも、IMPORTS に飲まれないこと。"""
        for label, head in ROOT_SHAPES.items():
            with self.subTest(shape=label):
                resolver, path = self._resolver(MODULE_WITH_IMPORTS % head)
                extracted = [tuple(d[:3]) for d
                             in resolver._extract_mib_definitions(path)]
                self.assertIn(("myMib", "enterprises", "99999"), extracted,
                              "根が抽出から消えた: %s" % extracted)
                self.assertNotIn("IMPORTS", [d[0] for d in extracted],
                                 "IMPORTS が定義として拾われている: %s"
                                 % extracted)
                self.assertEqual(resolver.resolve_name("myMib"),
                                 "1.3.6.1.4.1.99999")
                self.assertEqual(resolver.resolve_name("myLeaf"),
                                 "1.3.6.1.4.1.99999.1")

    def test_the_child_of_such_a_root_is_not_shown_under_a_fake_name(self):
        """配下の OID が `IMPORTS.1` のような偽名で表示されないこと。"""
        resolver, _ = self._resolver(
            MODULE_WITH_IMPORTS % ROOT_SHAPES["wrapped_indent"])
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.99999"), "myMib")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.99999.1"),
                         "myLeaf")
        self.assertNotIn("IMPORTS", resolver.oid_to_name.values())

    def test_a_wrapped_syntax_clause_does_not_cut_the_object_type(self):
        """`SYNTAX` 改行 `OBJECT IDENTIFIER` で本体が切れないこと。"""
        resolver, path = self._resolver(WRAPPED_SYNTAX)
        extracted = [tuple(d[:3]) for d
                     in resolver._extract_mib_definitions(path)]
        self.assertIn(("snmpTrapOID", "snmpTrap", "1"), extracted,
                      "snmpTrapOID が抽出から消えた: %s" % extracted)
        self.assertEqual(resolver.resolve_name("snmpTrapOID"),
                         "1.3.6.1.4.1.99999.4.1")


if __name__ == "__main__":
    unittest.main()
