r"""ラベル付きの 1 段の右辺（`{ 親 名前(番号) }`）を、1.3.1 から読めた 1 段と区別することを検証する。

1.3.1 が読めた右辺は `{ 親 番号 }` だけで、`{ system sn(5) }` のように添字に
ラベルが付いた形は定義ごと落ちていた。1.3.2 は右辺を全部読む
（_parse_oid_value）ので、この形も `{ system 5 }` と同じ 1 段として抜き
出す。ところが解決側は 1 段かどうかを添字の形（'.' の有無）だけで見て
いたので、1.3.1 に無かったこの宣言が 1.3.1 の 1 段と同じ扱いを受けた。
実測（21000e0）:
- 借用の回: ACME-MIB が system を読めない右辺（`{ acmeRoot acmeSub 9 }`）で
  宣言し、`acmeLabeled ::= { system sn(5) }` を置くと、標準の
  1.3.6.1.2.1.1.5 に 'acmeLabeled' が付いた（標準の OID に自社の名前。
  知らせは 0 行）。1.3.1 と 1.3.0 は名前なし。同じ MIB の複数添字の子
  `{ system 6 1 }` は、21000e0 で名前なしに直っている。
- IMPORTS の候補: ACME-MIB が `system ::= { acmeRoot sys(9) }` を置くと、
  SNMPv2-MIB の system を取り込んだ Z-MIB の zAlarm が、読む順 A,Z で
  1.3.6.1.4.1.777.9.99 に付いた（標準の 1.3.6.1.2.1.1.99 は 'system.99'）。
  1.3.1 と 1.3.0 はどの順でも 'zAlarm'。

直し方: 添字にラベルが 1 つでもある右辺は、複数添字と同じく 1.3.1 では
読めなかった形として扱う（借用に乗せない。同名の候補がほかにあれば、
よそのモジュールから名前で引く表へ入れない）。自分のモジュールの子の
親としてはこれまでどおり使う。
"""
import contextlib
import io
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


Z_STANDARD = mib("Z-MIB DEFINITIONS ::= BEGIN",
                 "IMPORTS system FROM SNMPv2-MIB;",
                 "zAlarm OBJECT IDENTIFIER ::= { system 99 }")
ORDERS_2 = (("A.my", "Z.my"), ("Z.my", "A.my"))


class LabeledSingleStepValueTest(unittest.TestCase):
    def _resolver(self, files, order=None):
        """exe の隣に mibs/ を作り、files を order の順で一覧させて読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-labeled-step-")
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
            if order is not None and os.path.normcase(
                    os.path.abspath(path)) == os.path.normcase(mibs):
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

    def test_a_labeled_child_does_not_borrow_a_standard_parent(self):
        """親が自分のモジュールで決まらないラベル付きの子は、標準の同名の
        下に付かないこと。1.3.1 と同じ `{ 親 番号 }` の子の借用は変えない。"""
        acme = mib("ACME-MIB DEFINITIONS ::= BEGIN",
                   "acmeRoot OBJECT IDENTIFIER ::= { enterprises 777 }",
                   "system OBJECT IDENTIFIER ::= { acmeRoot acmeSub 9 }",
                   "acmeSingle OBJECT IDENTIFIER ::= { system 4 }",
                   "acmeLabeled OBJECT IDENTIFIER ::= { system sn(5) }")
        r, _ = self._resolver({"ACME.my": acme})
        self.assertNotEqual(r.resolve_oid("1.3.6.1.2.1.1.5"), "acmeLabeled",
                            "標準の OID に自社の名前が付いている")
        self.assertIsNone(r.resolve_name("acmeLabeled"))
        # 対照: 1.3.1 から読めた 1 段の子は、これまでどおり借用する
        self.assertEqual(r.resolve_name("acmeSingle"), "1.3.6.1.2.1.1.4")

    def test_an_imported_standard_name_is_not_taken_by_a_labeled_copy(self):
        """ラベル付きの 1 段で自社の木に system を置き直した MIB があっても、
        SNMPv2-MIB の system を取り込んだ側の子が標準の OID に付くこと
        （読む順によらない）。"""
        acme = mib("ACME-MIB DEFINITIONS ::= BEGIN",
                   "acmeRoot OBJECT IDENTIFIER ::= { enterprises 777 }",
                   "system OBJECT IDENTIFIER ::= { acmeRoot sys(9) }",
                   "acmeSysName OBJECT IDENTIFIER ::= { system 5 }")
        for order in ORDERS_2:
            with self.subTest(order=",".join(order)):
                r, output = self._resolver({"A.my": acme, "Z.my": Z_STANDARD},
                                           order)
                self.assertEqual(r.resolve_name("zAlarm"), "1.3.6.1.2.1.1.99")
                self.assertEqual(r.resolve_oid("1.3.6.1.2.1.1.99"), "zAlarm")
                self.assertNotEqual(r.resolve_oid("1.3.6.1.4.1.777.9.99"),
                                    "zAlarm",
                                    "自社の木に Z の名前が付いている")
                # ACME 自身の子は自社の木に付く
                self.assertEqual(r.resolve_oid("1.3.6.1.4.1.777.9.5"),
                                 "acmeSysName")
                self.assertEqual(self._notices(output), [])

    def test_a_labeled_child_of_its_own_modules_parent_is_named(self):
        """親が自分のモジュールで決まるラベル付きの子と、その下の子には、
        これまでどおり名前が付くこと。"""
        acme = mib("ACME-MIB DEFINITIONS ::= BEGIN",
                   "acmeRoot OBJECT IDENTIFIER ::= { enterprises 777 }",
                   "acmeSub OBJECT IDENTIFIER ::= { acmeRoot sub(3) }",
                   "acmeLeaf OBJECT IDENTIFIER ::= { acmeSub 1 }")
        r, output = self._resolver({"ACME.my": acme})
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.777.3"), "acmeSub")
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.777.3.1"), "acmeLeaf")
        self.assertEqual(self._notices(output), [])

    def test_a_unique_labeled_name_is_found_by_other_modules(self):
        """同名の候補がほかに無ければ、ラベル付きの宣言も、取り込んだ
        モジュールの子の親になること。"""
        acme = mib("ACME-MIB DEFINITIONS ::= BEGIN",
                   "acmeRoot OBJECT IDENTIFIER ::= { enterprises 777 }",
                   "acmeSub OBJECT IDENTIFIER ::= { acmeRoot sub(3) }")
        z_mib = mib("Z-MIB DEFINITIONS ::= BEGIN",
                    "IMPORTS acmeSub FROM ACME-MIB;",
                    "zLeaf OBJECT IDENTIFIER ::= { acmeSub 7 }")
        for order in ORDERS_2:
            with self.subTest(order=",".join(order)):
                r, output = self._resolver({"A.my": acme, "Z.my": z_mib},
                                           order)
                self.assertEqual(r.resolve_name("zLeaf"),
                                 "1.3.6.1.4.1.777.3.7")
                self.assertEqual(self._notices(output), [])


if __name__ == "__main__":
    unittest.main()
