r"""コメントの中の「BOM + 名前 DEFINITIONS」で幽霊の見出しができないことを検証する。

本文に残る BOM（U+FEFF）は「直後にモジュールの見出しが続くもの」だけを
改行へ置き換えている。その判定は行の途中でも当たるので、`--` コメントの
中に `<BOM>NAME DEFINITIONS` の並びがあると、そこでコメントが切れて
残りが生きたコードになり、幽霊のモジュール見出しができる。MIB は
「別モジュールへ移した」という由来をコメントで残すのが普通なので、Web や
PDF から貼った MIB に U+FEFF が 1 つ混ざるだけで当たる。

実測（alarmA の本体に
`    -- moved from <BOM>OLD-MIB DEFINITIONS ::= BEGIN ::= { bogus 9 }`
を 1 行入れる）:
- コメントがその BOM で終わり、`OLD-MIB DEFINITIONS ::= BEGIN ...` が
  行頭の見出しになる。alarmA の定義はその手前で区間ごと打ち切られ、
  `::= { aRoot 1 }` へ届かない。
- resolve_name("alarmA") が None になる。BOM を抜いた同じ MIB は
  1.3.6.1.4.1.1111.1 で正しいので、BOM があるときだけ静かに壊れる。
- 4 周目（BOM を無条件で改行にしていた頃）でも同じなので、直近の修正が
  持ち込んだものではない。

直し方: 置き換えを決める前にコメントと文字列の位置を調べ、コメント・
文字列の中の BOM は「その行の残りがモジュールの見出しちょうど」のとき
だけ区切りとして扱う。連結したファイルの先頭に残る BOM（コメントの外、
または末尾コメントの直後に見出しだけが続く形）はこれまでどおり改行に
なるので、既存の連結の区切りは変わらない。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# ソースへ直に書くと目に見えないので番号から作る
BOM = chr(0xFEFF)

# 「別モジュールから移した」という由来のコメントに BOM が 1 つ混ざった MIB
GHOST_HEADER_IN_COMMENT = (
    "A-MIB DEFINITIONS ::= BEGIN\r\n"
    "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
    "bogus OBJECT IDENTIFIER ::= { enterprises 9999 }\r\n"
    "alarmA NOTIFICATION-TYPE\r\n"
    "    STATUS current\r\n"
    "    -- moved from " + BOM + "OLD-MIB DEFINITIONS ::= BEGIN "
    "::= { bogus 9 }\r\n"
    "    ::= { aRoot 1 }\r\n"
    "liveNode OBJECT IDENTIFIER ::= { aRoot 2 }\r\n"
    "END\r\n")

# 見出しだけをコメントに書いた形（行の残りが見出しちょうど）でも、
# それが本文の途中なら幽霊にしない
GHOST_HEADER_AT_END_OF_COMMENT = (
    "A-MIB DEFINITIONS ::= BEGIN\r\n"
    "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
    "-- see also " + BOM + "OLD-MIB\r\n"
    "liveNode OBJECT IDENTIFIER ::= { aRoot 2 }\r\n"
    "END\r\n")


class MibBomGhostHeaderInCommentTest(unittest.TestCase):
    def _resolver(self, text):
        """exe の隣の mibs/ へ A.my を置いて読み込む"""
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-bom-ghost-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(exe_dir, "mibs"))
        path = os.path.join(exe_dir, "mibs", "A.my")
        with open(path, "wb") as f:
            f.write(text.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        return MIBResolver(), path

    def test_a_header_inside_a_comment_does_not_cut_the_comment(self):
        """コメントの中の見出しで、後ろの定義が打ち切られないこと。"""
        resolver, path = self._resolver(GHOST_HEADER_IN_COMMENT)
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1",
                         "コメントの中の見出しで alarmA が落ちている")
        self.assertEqual(resolver.resolve_name("liveNode"),
                         "1.3.6.1.4.1.1111.2")
        modules = {module for _, _, _, module
                   in resolver._extract_mib_definitions(path)}
        self.assertEqual(modules, {"A-MIB"},
                         "幽霊のモジュールができている: %r" % modules)

    def test_the_same_mib_without_the_bom_resolves_the_same_way(self):
        """BOM を抜いた同じ MIB と、結果が食い違わないこと。"""
        resolver, _ = self._resolver(
            GHOST_HEADER_IN_COMMENT.replace(BOM, ""))
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1")
        self.assertEqual(resolver.resolve_name("liveNode"),
                         "1.3.6.1.4.1.1111.2")

    def test_a_bare_module_name_in_a_comment_stays_a_comment(self):
        """見出しになりきらない並びでも、コメントのままであること。"""
        resolver, path = self._resolver(GHOST_HEADER_AT_END_OF_COMMENT)
        self.assertEqual(resolver.resolve_name("liveNode"),
                         "1.3.6.1.4.1.1111.2")
        modules = {module for _, _, _, module
                   in resolver._extract_mib_definitions(path)}
        self.assertEqual(modules, {"A-MIB"})


if __name__ == "__main__":
    unittest.main()
