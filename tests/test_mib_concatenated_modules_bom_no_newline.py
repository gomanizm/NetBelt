"""1 つ目の MIB に末尾の改行が無い BOM 付き連結でも、モジュールを区切れることを検証する。

モジュールの見出し（`X DEFINITIONS ::= BEGIN`）を探す正規表現には、行頭の
BOM（U+FEFF）を空白と同じく読み飛ばす前置きを足してあった。しかしそれは
BOM が行頭にあるときにしか効かない。`copy /b A.my+B.my AB.my` のように
生のまま連結すると、A.my に末尾の改行が無ければ `END` の直後に次の BOM が
来るので、2 つ目の見出しは行頭から始まらず、前置きに当たらない。

実測（A-MIB: aRoot=enterprises.1111・shared={aRoot 1}・alarmA={shared 1}、
B-MIB: bRoot=enterprises.2222・shared={bRoot 1}・alarmB={shared n}。
A.my の末尾 CRLF を取ってから BOM 付きで連結）:
- モジュールは ['A-MIB'] の 1 つだけになり、B の定義まで 'A-MIB' 扱いになる。
- 同じ名前 shared が後勝ちで B のものになり、alarmA が B の enterprise の下、
  1.3.6.1.4.1.2222.1.1 として解決される（A の Trap 名が B の OID に付く）。
- BOM の無い同じ連結は ['A-MIB', 'ENDB-MIB'] に分かれ、OID は正しい。
  つまり BOM が付いているときだけ静かに壊れる。

直し方: ファイルを読んだ直後（_blank_comments_and_strings に渡す前）に、
BOM を改行 1 文字へ置き換えて正規化する。1 文字→1 文字なので、あとで
位置を使う処理がずれない。抽出の規則が変わるので MIB_PARSER_VERSION を
上げた。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

BOM = b"\xef\xbb\xbf"

# 末尾に改行を置かない（copy /b で次のファイルが直接くっつく形）
A_MIB_NO_EOL = ("A-MIB DEFINITIONS ::= BEGIN\r\n"
                "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
                "shared OBJECT IDENTIFIER ::= { aRoot 1 }\r\n"
                "alarmA NOTIFICATION-TYPE\r\n"
                "    STATUS current\r\n"
                "    ::= { shared 1 }\r\n"
                "END").encode("utf-8")


def _b_mib(trap_index):
    return ("B-MIB DEFINITIONS ::= BEGIN\r\n"
            "bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }\r\n"
            "shared OBJECT IDENTIFIER ::= { bRoot 1 }\r\n"
            "alarmB NOTIFICATION-TYPE\r\n"
            "    STATUS current\r\n"
            "    ::= { shared %d }\r\n"
            "END\r\n" % trap_index).encode("utf-8")


class MibConcatenatedModulesBomNoNewlineTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かし、
        # MIBResolver() を丸ごと作る（利用者が mibs/ に置いて起動する経路）
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-concat-bom-eol-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(self.exe_dir, "mibs"))

    def _write(self, name, data):
        path = os.path.join(self.exe_dir, "mibs", name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_a_bom_right_after_end_still_starts_a_new_module(self):
        """END の直後に次の BOM が来ても、Trap が自分の親に付くこと。"""
        for trap_index in (1, 2):
            with self.subTest(trap_index=trap_index):
                # 前の場合のキャッシュを使わせない
                cache = os.path.join(self.exe_dir, "mib_cache.json")
                if os.path.exists(cache):
                    os.remove(cache)
                path = self._write(
                    "AB.my", BOM + A_MIB_NO_EOL + BOM + _b_mib(trap_index))
                resolver = self._resolver()
                self.assertEqual(resolver.resolve_name("alarmA"),
                                 "1.3.6.1.4.1.1111.1.1",
                                 "A の Trap が A の shared に付いていない")
                self.assertEqual(
                    resolver.resolve_oid("1.3.6.1.4.1.1111.1.1"), "alarmA")
                self.assertEqual(
                    resolver.resolve_oid("1.3.6.1.4.1.2222.1.%d" % trap_index),
                    "alarmB")
                modules = {name: module for name, _, _, module
                           in resolver._extract_mib_definitions(path)
                           if name in ("aRoot", "alarmA", "bRoot", "alarmB")}
                self.assertEqual(modules,
                                 {"aRoot": "A-MIB", "alarmA": "A-MIB",
                                  "bRoot": "B-MIB", "alarmB": "B-MIB"})


if __name__ == "__main__":
    unittest.main()
