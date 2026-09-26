r"""1.3.1 では決まらなかった節を取り込んだよそのモジュールの 1 段の宣言が、IMPORTS の既知の制限を広げないことを検証する（読む順 6 通り）。

既知の制限: IMPORTS を見ないので、2 つの候補（内蔵表の名前とモジュールの
同名など）があるとき、その名前を取り込んだ第三のモジュールの子は、後に
決まった方の下に付く。1.3.2 では、1.3.1 で OID が決まらなかった宣言（fresh:
右辺が 1 段でないもの・ラベル付きフルパスの根の下のもの。子孫まで）を、同名
の候補がほかにあるとき、よそのモジュールから名前で引く表（known）へ入れない。

fresh は同じモジュールの中（fresh_in_module）と、モジュールをまたぐ所
（fresh_known。else 節の `parent_fresh = parent in fresh_known`）の 2 か所で
子へ伝わる。これまでのテスト（test_mib_child_of_a_newly_read_parent_and_imports.py）
は根と system が同じモジュールにある形しか見ておらず、モジュールをまたぐ
所を外しても MIB の全テストが通っていた（1 周目の検査役が変種で実測）。
その変種では次の形で後退した: A-MIB がラベル付きフルパスの根の下に一意な
aNode を置き、B-MIB が `IMPORTS aNode FROM A-MIB;` のうえで
`system ::= { aNode 9 }` を置き、Z-MIB が `IMPORTS system FROM SNMPv2-MIB;`
のうえで `zAlarm ::= { system 99 }` を置くと、読む順 A,B,Z で zAlarm が
他社の 1.3.6.1.4.1.777.2.9.99 に付き、標準の 1.3.6.1.2.1.1.99 は
'system.99' になった（知らせは 0 行）。1.3.1・1.3.0・ebbe593 はどの順でも
'zAlarm'。

本体は直さない（ebbe593 で正しい）。この形を見張りとして足し、モジュールを
またいで fresh を伝える所が外れたら落ちるようにする。
"""
import contextlib
import io
import itertools
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

NL = "\r\n"


def mib(*lines):
    return NL.join(lines + ("END", ""))


A_MIB = mib("A-MIB DEFINITIONS ::= BEGIN",
            "aRoot OBJECT IDENTIFIER ::= { iso(1) org(3) dod(6) internet(1)"
            " private(4) enterprises(1) 777 }",
            "aNode OBJECT IDENTIFIER ::= { aRoot 2 }")
B_MIB = mib("B-MIB DEFINITIONS ::= BEGIN",
            "IMPORTS aNode FROM A-MIB;",
            "system OBJECT IDENTIFIER ::= { aNode 9 }",
            "bSysName OBJECT IDENTIFIER ::= { system 5 }")
Z_MIB = mib("Z-MIB DEFINITIONS ::= BEGIN",
            "IMPORTS system FROM SNMPv2-MIB;",
            "zAlarm OBJECT IDENTIFIER ::= { system 99 }")
FILES = {"A.my": A_MIB, "B.my": B_MIB, "Z.my": Z_MIB}
ORDERS = tuple(itertools.permutations(sorted(FILES)))


class FreshMarkCrossesImportsTest(unittest.TestCase):
    def _resolver(self, files, order):
        """exe の隣に mibs/ を作り、files を order の順で一覧させて読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-fresh-imports-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        mibs = os.path.join(exe_dir, "mibs")
        os.makedirs(mibs)
        for name, text in files.items():
            with open(os.path.join(mibs, name), "wb") as f:
                f.write(text.encode("utf-8"))
        real_listdir = os.listdir

        def listdir(path="."):
            got = real_listdir(path)
            if os.path.normcase(os.path.abspath(path)) == \
                    os.path.normcase(mibs):
                self.assertEqual(sorted(got), sorted(order))
                return list(order)
            return got

        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe),
                      mock.patch("os.listdir", listdir)):
            patch.start()
            self.addCleanup(patch.stop)
        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resolver = MIBResolver()
        return resolver, out.getvalue()

    @staticmethod
    def _notices(output):
        return [line for line in output.splitlines() if "解決せず" in line]

    def test_a_standard_import_is_not_taken_by_a_copy_in_a_third_module(self):
        """よそのモジュールの fresh な節の下に 1 段で system を置き直した
        MIB があっても、SNMPv2-MIB の system を取り込んだ側の子が標準の
        OID に付くこと（読む順 6 通りのどれでも）。"""
        self.assertEqual(len(ORDERS), 6)
        for order in ORDERS:
            with self.subTest(order=",".join(order)):
                r, output = self._resolver(FILES, order)
                self.assertEqual(r.resolve_name("zAlarm"),
                                 "1.3.6.1.2.1.1.99")
                self.assertEqual(r.resolve_oid("1.3.6.1.2.1.1.99"), "zAlarm")
                self.assertNotEqual(
                    r.resolve_oid("1.3.6.1.4.1.777.2.9.99"), "zAlarm",
                    "他社の木に Z の名前が付いている")
                self.assertEqual(self._notices(output), [])

    def test_the_copy_still_names_its_own_children(self):
        """B-MIB 自身の子は、自分の system（aNode の下）に付くこと
        （1.3.2 で付くようになった分を減らさない。読む順 6 通りのどれでも）。"""
        for order in ORDERS:
            with self.subTest(order=",".join(order)):
                r, _ = self._resolver(FILES, order)
                self.assertEqual(r.resolve_name("aNode"), "1.3.6.1.4.1.777.2")
                self.assertEqual(r.resolve_name("bSysName"),
                                 "1.3.6.1.4.1.777.2.9.5")
                self.assertEqual(r.resolve_oid("1.3.6.1.4.1.777.2.9.5"),
                                 "bSysName")


if __name__ == "__main__":
    unittest.main()
