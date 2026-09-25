r"""BOM を改行へ置き換える先読みが、見出しの正規表現と同じ形を受けることを検証する。

BOM 付きの MIB を生のまま連結したとき、「直後にモジュールの見出しが続く
BOM」だけを改行へ置き換えるようにした。その先読みは
`(?=[ \t]*[\w-]+[ \t]+DEFINITIONS\b)` で、見出しを探す _MIB_MODULE_HEADER
（`^[ \t<BOM>]*([\w-]+)\s+DEFINITIONS\b`）より狭かった。見出しは名前と
DEFINITIONS の間に改行を許すのに、先読みは同じ行のうちの空白とタブしか
許さない。

実測（A.my に末尾の改行が無い BOM 連結 + B.my の見出しが
`B-MIB` と `DEFINITIONS ::= BEGIN` で行が折れている形）:
- 見出しは ['A-MIB'] の 1 つしか見つからず、B の定義まで 'A-MIB' 扱いになる。
- 同じ名前 shared が後勝ちで B のものになり、alarmA が B の enterprise の
  下（1.3.6.1.4.1.2222.1.1）へ付く。
- 前のモジュールに末尾の改行があると、BOM が行頭に来るので見出しが
  行頭から始まり、同じ折れ方でも正しく区切れる。つまり「末尾の改行が
  無い」ときだけ静かに壊れる。

直し方: 先読みを見出しの正規表現と同じ許容範囲まで広げる
（`[ \t<BOM>]*[\w-]+[\s<BOM>]+DEFINITIONS\b`）。
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
BOM_BYTES = BOM.encode("utf-8")

A_BODY = ("aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
          "shared OBJECT IDENTIFIER ::= { aRoot 1 }\r\n"
          "alarmA NOTIFICATION-TYPE\r\n"
          "    STATUS current\r\n"
          "    ::= { shared 1 }\r\n"
          "END")

B_BODY = ("bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }\r\n"
          "shared OBJECT IDENTIFIER ::= { bRoot 1 }\r\n"
          "alarmB NOTIFICATION-TYPE\r\n"
          "    STATUS current\r\n"
          "    ::= { shared 2 }\r\n"
          "END\r\n")

# 見出しの書き方。実 MIB は名前と DEFINITIONS の間で行を折ることがある
HEADER_SHAPES = {
    # 1 行（これまで通る形）
    "one_line": "B-MIB DEFINITIONS ::= BEGIN\r\n",
    # 名前の直後で折る
    "wrapped": "B-MIB\r\nDEFINITIONS ::= BEGIN\r\n",
    # 折ったうえに次の行を字下げする
    "wrapped_indent": "B-MIB\r\n    DEFINITIONS ::= BEGIN\r\n",
}


class MibBomHeaderLookaheadWidthTest(unittest.TestCase):
    def _resolver(self, data):
        """exe の隣の mibs/ へ AB.my を置いて読み込む

        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-bom-lookahead-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(exe_dir, "mibs"))
        path = os.path.join(exe_dir, "mibs", "AB.my")
        with open(path, "wb") as f:
            f.write(data)
        from core.mib_resolver import MIBResolver
        return MIBResolver(), path

    def _check(self, resolver, path):
        """A と B が別のモジュールとして区切られていること"""
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1.1",
                         "A の Trap が A の shared に付いていない")
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.2")
        modules = {name: module for name, _, _, module
                   in resolver._extract_mib_definitions(path)
                   if name in ("aRoot", "alarmA", "bRoot", "alarmB")}
        self.assertEqual(modules,
                         {"aRoot": "A-MIB", "alarmA": "A-MIB",
                          "bRoot": "B-MIB", "alarmB": "B-MIB"})

    def test_every_header_shape_splits_when_the_previous_module_has_no_eol(self):
        """末尾の改行が無い連結でも、どの見出しの形でも区切れること。"""
        for label, header in HEADER_SHAPES.items():
            with self.subTest(shape=label):
                data = (BOM_BYTES
                        + ("A-MIB DEFINITIONS ::= BEGIN\r\n"
                           + A_BODY).encode("utf-8")
                        + BOM_BYTES + (header + B_BODY).encode("utf-8"))
                resolver, path = self._resolver(data)
                self._check(resolver, path)

    def test_every_header_shape_splits_when_the_previous_module_ends_with_eol(self):
        """末尾の改行がある連結は、これまでどおり区切れること（対照）。"""
        for label, header in HEADER_SHAPES.items():
            with self.subTest(shape=label):
                data = (BOM_BYTES
                        + ("A-MIB DEFINITIONS ::= BEGIN\r\n"
                           + A_BODY + "\r\n").encode("utf-8")
                        + BOM_BYTES + (header + B_BODY).encode("utf-8"))
                resolver, path = self._resolver(data)
                self._check(resolver, path)


if __name__ == "__main__":
    unittest.main()
