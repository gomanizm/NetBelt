"""BOM 付きの MIB を連結したファイルでも、モジュールごとに区切れることを検証する。

モジュールの見出し（`X DEFINITIONS ::= BEGIN`）は行頭の空白だけを許す
正規表現で探していた。ファイルは encoding='utf-8' で開くので、BOM は
U+FEFF の文字として本文に残り、その直後の見出しは見出しと認められない。
BOM 付きの A.my と B.my を `copy /b A.my+B.my AB.my` で連結すると各モジュールの
先頭に BOM が残る。実測（A-MIB: aRoot=enterprises.1111・shared={aRoot 1}・
alarmA={shared 1}、B-MIB: bRoot=enterprises.2222・shared={bRoot 1}・
alarmB={shared n}）:
- 両方に BOM・n=1: 全定義がファイル名 'AB.my' の 1 モジュール扱いになり、
  alarmA が解決されず（None）、1.3.6.1.4.1.1111.1.1 は 'shared.1'。
- 両方に BOM・n=2: alarmA が B の shared に付いて 1.3.6.1.4.1.2222.1.1 になり、
  B の OID 2222.1.1 が A の Trap 名 'alarmA' と表示される。
- 先頭だけに BOM（PowerShell 5.1 の Set-Content -Encoding UTF8 で連結した形）:
  最初の見出しを見落とし、A の定義まで 'B-MIB' 扱いになって同じ結果。
BOM の無い同じ内容は正しく解決する。encoding='utf-8-sig' で開いても消えるのは
ファイル先頭の BOM だけで、途中のモジュールの BOM は残る。

直し方: 見出しの正規表現の前置き（行頭の空白）に U+FEFF も許す。抽出の規則が
変わるので MIB_PARSER_VERSION を上げた。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

BOM = b"\xef\xbb\xbf"

A_MIB = ("A-MIB DEFINITIONS ::= BEGIN\r\n"
         "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
         "shared OBJECT IDENTIFIER ::= { aRoot 1 }\r\n"
         "alarmA NOTIFICATION-TYPE\r\n"
         "    STATUS current\r\n"
         "    ::= { shared 1 }\r\n"
         "END\r\n").encode("utf-8")


def _b_mib(trap_index):
    return ("B-MIB DEFINITIONS ::= BEGIN\r\n"
            "bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }\r\n"
            "shared OBJECT IDENTIFIER ::= { bRoot 1 }\r\n"
            "alarmB NOTIFICATION-TYPE\r\n"
            "    STATUS current\r\n"
            "    ::= { shared %d }\r\n"
            "END\r\n" % trap_index).encode("utf-8")


class MibConcatenatedModulesBomTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かし、
        # MIBResolver() を丸ごと作る（利用者が mibs/ に置いて起動する経路）
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-concat-bom-")
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

    def test_modules_each_starting_with_a_bom_keep_their_own_parent(self):
        """各モジュールの先頭に BOM が残る連結でも、Trap が自分の親に付くこと。"""
        cases = {
            "BOM before each module": lambda b: BOM + A_MIB + b"\r\n" + BOM + b,
            "BOM only at the start": lambda b: BOM + A_MIB + b"\r\n" + b,
        }
        for label, join in cases.items():
            for trap_index in (1, 2):
                with self.subTest(label, trap_index=trap_index):
                    # 前の場合のキャッシュを使わせない
                    cache = os.path.join(self.exe_dir, "mib_cache.json")
                    if os.path.exists(cache):
                        os.remove(cache)
                    path = self._write("AB.my", join(_b_mib(trap_index)))
                    resolver = self._resolver()
                    self.assertEqual(resolver.resolve_name("alarmA"),
                                     "1.3.6.1.4.1.1111.1.1",
                                     "A の Trap が A の shared に付いていない")
                    self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.1111.1.1"),
                                     "alarmA")
                    self.assertEqual(
                        resolver.resolve_oid("1.3.6.1.4.1.2222.1.%d" % trap_index),
                        "alarmB")
                    modules = {name: module for name, _, _, module
                               in resolver._extract_mib_definitions(path)
                               if name in ("aRoot", "alarmA", "bRoot", "alarmB")}
                    self.assertEqual(modules, {"aRoot": "A-MIB", "alarmA": "A-MIB",
                                               "bRoot": "B-MIB", "alarmB": "B-MIB"})


if __name__ == "__main__":
    unittest.main()
