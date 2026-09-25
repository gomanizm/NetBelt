"""コメントの中の BOM（U+FEFF）でコメントが終わらないことを検証する。

「BOM 付きの MIB を生のまま連結すると見出しを見落とす」を直したとき、
読み込んだ直後に BOM を無条件で改行へ置き換えるようにした。そのため
`--` コメントの中に U+FEFF が 1 つ混ざると、そこで行が終わってコメントが
閉じ、残りが生きたコードになる。_blank_comments_and_strings が防いで
いた「コメントの中の `::= { x n }` が偽の親になる」が復活していた。

実測:
- alarmA の本体に `    -- was <U+FEFF> ::= { bogus 9 }` を 1 行入れると、
  alarmA がコメントの中の bogus の下（1.3.6.1.4.1.9999.9）になる。BOM を
  抜いた同じ MIB は 1.3.6.1.4.1.1111.1 で正しいので、BOM があるときだけ
  静かに壊れる。
- コメントアウト済みの定義（`-- <U+FEFF> ghostNode OBJECT IDENTIFIER
  ::= { aRoot 7 }`）は、名前→OID に幽霊として増える。MIB は廃止した
  オブジェクトをコメントで残すのが普通なので、Web や PDF から貼った MIB に
  U+FEFF が 1 つ混ざるだけで当たる。
- 見出しの途中（`B-MIB<U+FEFF> DEFINITIONS ::= BEGIN`）に混ざった BOM も、
  改行になるせいで見出しを壊し、次のモジュールが前のモジュール扱いになる。

直し方: BOM は「直後にモジュールの見出しが続くもの」だけ改行へ、それ以外は
空白へ置き換える（どちらも 1 文字→1 文字なので、あとで位置を使う処理が
ずれない）。連結を区切る働きは残したまま、コメントと文字列の中では無害な
空白になる。抽出の規則が変わるので MIB_PARSER_VERSION を上げた。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 本文へ紛れ込む U+FEFF。ソースへ直に書くと目に見えないので番号から作る
BOM = chr(0xFEFF)
BOM_BYTES = BOM.encode("utf-8")

# コメントの中に BOM が 1 つ混ざった MIB。廃止した親をコメントで残した形
PARENT_IN_COMMENT = ("A-MIB DEFINITIONS ::= BEGIN\r\n"
                     "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
                     "bogus OBJECT IDENTIFIER ::= { enterprises 9999 }\r\n"
                     "alarmA NOTIFICATION-TYPE\r\n"
                     "    -- was " + BOM + " ::= { bogus 9 }\r\n"
                     "    STATUS current\r\n"
                     "    ::= { aRoot 1 }\r\n"
                     "END\r\n")

# コメントアウトした定義の中に BOM が 1 つ混ざった MIB
GHOST_IN_COMMENT = ("A-MIB DEFINITIONS ::= BEGIN\r\n"
                    "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
                    "-- " + BOM + " ghostNode OBJECT IDENTIFIER "
                    "::= { aRoot 7 }\r\n"
                    "liveNode OBJECT IDENTIFIER ::= { aRoot 1 }\r\n"
                    "END\r\n")


def _module(name, root_index, trap_index):
    """`<name>-MIB` 1 モジュール分の本文（末尾の改行つき）"""
    return ("%s-MIB DEFINITIONS ::= BEGIN\r\n"
            "%sRoot OBJECT IDENTIFIER ::= { enterprises %d }\r\n"
            "shared OBJECT IDENTIFIER ::= { %sRoot 1 }\r\n"
            "alarm%s NOTIFICATION-TYPE\r\n"
            "    STATUS current\r\n"
            "    ::= { shared %d }\r\n"
            "END\r\n" % (name, name.lower(), root_index, name.lower(),
                         name, trap_index))


class MibBomInsideCommentTest(unittest.TestCase):
    def _resolver(self, files):
        """exe の隣に mibs/ を作り、files（名前→中身）を置いて読み込む

        利用者が mibs/ へ MIB を置いて起動する経路をそのまま通す。
        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-bom-comment-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(exe_dir, "mibs"))
        for name, text in files.items():
            with open(os.path.join(exe_dir, "mibs", name), "wb") as f:
                f.write(text.encode("utf-8") if isinstance(text, str)
                        else text)
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_a_bom_inside_a_comment_does_not_revive_the_rest_of_it(self):
        """コメントの中の BOM で、コメントの中の親が生き返らないこと。"""
        resolver = self._resolver({"A.my": PARENT_IN_COMMENT})
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1",
                         "コメントの中の bogus が親になっている")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.1111.1"), "alarmA")
        self.assertNotIn("1.3.6.1.4.1.9999.9", resolver.oid_to_name)

    def test_the_same_mib_without_the_bom_resolves_the_same_way(self):
        """BOM を抜いた同じ MIB と、結果が食い違わないこと。"""
        resolver = self._resolver(
            {"A.my": PARENT_IN_COMMENT.replace(BOM, "")})
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1")

    def test_a_commented_out_definition_with_a_bom_stays_dead(self):
        """コメントアウト済みの定義が、幽霊として名前→OID に増えないこと。"""
        resolver = self._resolver({"A.my": GHOST_IN_COMMENT})
        self.assertIsNone(resolver.resolve_name("ghostNode"),
                          "コメントの中の定義が生きている")
        self.assertNotIn("ghostNode", resolver.oid_to_name.values())
        # コメントの外の定義はこれまでどおり読めること
        self.assertEqual(resolver.resolve_name("liveNode"),
                         "1.3.6.1.4.1.1111.1")

    def test_three_concatenated_modules_each_keep_their_own_parent(self):
        """BOM 付き 3 つを連結しても、各 Trap が自分の親に付くこと。"""
        data = (BOM_BYTES + _module("A", 1111, 1).encode("utf-8")
                + BOM_BYTES + _module("B", 2222, 2).encode("utf-8")
                + BOM_BYTES + _module("C", 3333, 3).encode("utf-8"))
        resolver = self._resolver({"ABC.my": data})
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1.1")
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.2")
        self.assertEqual(resolver.resolve_name("alarmC"),
                         "1.3.6.1.4.1.3333.1.3")

    def test_a_bom_right_after_a_trailing_comment_starts_a_new_module(self):
        """前のファイルがコメントで終わっていても、見出しを見つけること。"""
        head = _module("A", 1111, 1).rstrip("\r\n") + "\r\n-- 末尾のコメント"
        data = (BOM_BYTES + head.encode("utf-8")
                + BOM_BYTES + _module("B", 2222, 2).encode("utf-8"))
        resolver = self._resolver({"AB.my": data})
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1.1")
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.2")

    def test_a_bom_right_after_a_bare_cr_starts_a_new_module(self):
        """前のファイルが CR だけで終わっていても、見出しを見つけること。"""
        head = _module("A", 1111, 1).rstrip("\r\n") + "\r"
        data = (BOM_BYTES + head.encode("utf-8")
                + BOM_BYTES + _module("B", 2222, 2).encode("utf-8"))
        resolver = self._resolver({"AB.my": data})
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1.1")
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.2")

    def test_a_bom_inside_the_header_line_does_not_break_the_header(self):
        """見出し行の途中に混ざった BOM でも、モジュールを区切れること。"""
        cases = {
            # 字下げと名前の間（連結したファイルの先頭に残った形）
            "indent": ("  " + BOM + "B-MIB DEFINITIONS", "B-MIB DEFINITIONS"),
            # 名前と DEFINITIONS の間（貼り付けで紛れ込んだ形）
            "name": ("B-MIB" + BOM + " DEFINITIONS", "B-MIB DEFINITIONS"),
        }
        for label, (broken, intact) in cases.items():
            with self.subTest(case=label):
                b = _module("B", 2222, 2).replace(intact, broken, 1)
                data = (BOM_BYTES + _module("A", 1111, 1).encode("utf-8")
                        + b.encode("utf-8"))
                resolver = self._resolver({"AB.my": data})
                self.assertEqual(resolver.resolve_name("alarmA"),
                                 "1.3.6.1.4.1.1111.1.1",
                                 "A の Trap が A の shared に付いていない")
                self.assertEqual(resolver.resolve_name("alarmB"),
                                 "1.3.6.1.4.1.2222.1.2")


if __name__ == "__main__":
    unittest.main()
