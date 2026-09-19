"""1 つのファイルに複数の MIB モジュールを連結しても、モジュールが混ざらない
ことを検証する。

モジュール名はファイルの最初の `X DEFINITIONS ::= BEGIN` から一度だけ取り、
ファイル全体から抜き出した定義すべてに同じ名前を付けていた。A-MIB
（aRoot=enterprises.1111、shared={aRoot 1}、alarmA={shared 1}）と B-MIB
（bRoot=enterprises.2222、shared={bRoot 1}）を 1 つの AB.my に連結すると、
6 件すべてが 'A-MIB' 扱いになり、2 つの shared が同じモジュールの同名として
後勝ちになる。実測では次のとおり。
- B に alarmB={shared 1} がある: alarmA が解決されず（None）、
  1.3.6.1.4.1.1111.1.1 は 'shared.1'。
- B の Trap が {shared 2}: B の Trap 1.3.6.1.4.1.2222.1.1 が A の Trap 名
  'alarmA' と表示される。
同じ 2 モジュールを A.my / B.my に分けると正しく解決する。標準出力の件数
（'AB.my: 5件'）は名前の有無で数えるので、取りこぼしは見えない。連結型の
MIB 一式を配るベンダーがあるので、利用者の操作で届く。

直し方: `X DEFINITIONS ::= BEGIN` の出現位置ごとに本文を区切り、区間ごとに
そのモジュール名を付けて抜き出す。最初の見出しより前の部分はこれまでどおり
最初のモジュールに含め、見出しが無いファイルはファイル名をモジュール名の
代わりにする（どちらも 1 モジュールのファイルの結果は変わらない）。抽出の
規則が変わるので MIB_PARSER_VERSION を上げた。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

A_MIB = ("A-MIB DEFINITIONS ::= BEGIN\n"
         "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\n"
         "shared OBJECT IDENTIFIER ::= { aRoot 1 }\n"
         "alarmA NOTIFICATION-TYPE\n"
         "    STATUS current\n"
         "    ::= { shared 1 }\n"
         "END\n")


def _b_mib(trap_index):
    return ("B-MIB DEFINITIONS ::= BEGIN\n"
            "bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }\n"
            "shared OBJECT IDENTIFIER ::= { bRoot 1 }\n"
            "alarmB NOTIFICATION-TYPE\n"
            "    STATUS current\n"
            "    ::= { shared %d }\n"
            "END\n" % trap_index)


class MibConcatenatedModulesTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かし、
        # MIBResolver() を丸ごと作る（利用者が mibs/ に置いて起動する経路）。
        # リポジトリ直下の custom_mibs.json に引きずられないようにするため。
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-concat-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(self.exe_dir, "mibs"))

    def _write(self, name, text):
        path = os.path.join(self.exe_dir, "mibs", name)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_each_module_in_one_file_keeps_its_own_parent(self):
        """連結したファイルでも、各 Trap が自分のモジュールの shared に付くこと。"""
        self._write("AB.my", A_MIB + "\n" + _b_mib(1))
        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1.1", "A の Trap が解決されない")
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.1")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.1111.1.1"),
                         "alarmA")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.2222.1.1"),
                         "alarmB")

    def test_another_modules_oid_is_not_shown_with_this_modules_name(self):
        """B の OID が A の Trap 名で表示されないこと（Trap 表示の経路）。"""
        self._write("AB.my", A_MIB + "\n" + _b_mib(2))
        resolver = self._resolver()
        self.assertNotEqual(resolver.resolve_oid("1.3.6.1.4.1.2222.1.1"),
                            "alarmA", "B の OID が A の Trap 名になった")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.1111.1.1"),
                         "alarmA")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.2222.1.2"),
                         "alarmB")

    def test_each_definition_carries_the_module_it_is_written_in(self):
        """抜き出した定義に、それが書かれたモジュールの名前が付くこと。"""
        path = self._write("AB.my", A_MIB + "\n" + _b_mib(1))
        modules = {name: module for name, _, _, module
                   in self._resolver()._extract_mib_definitions(path)
                   if name in ("aRoot", "alarmA", "bRoot", "alarmB")}
        self.assertEqual(modules, {"aRoot": "A-MIB", "alarmA": "A-MIB",
                                   "bRoot": "B-MIB", "alarmB": "B-MIB"})

    def test_split_files_still_resolve_the_same_way(self):
        """同じ 2 モジュールを別ファイルに置いた場合の結果が変わらないこと。"""
        self._write("A.my", A_MIB)
        self._write("B.my", _b_mib(1))
        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1.1")
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.1")


if __name__ == "__main__":
    unittest.main()
