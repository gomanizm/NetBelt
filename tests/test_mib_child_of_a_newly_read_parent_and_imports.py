r"""1.3.2 から読める右辺の下に 1 段で置いた宣言が、IMPORTS の既知の制限を広げないことを検証する。

既知の制限: IMPORTS を見ないので、2 つの候補（2 つのモジュールの同名、
または内蔵表・custom_mibs.json の名前とモジュールの同名）があるとき、
その名前を取り込んだ第三のモジュールの子は、後に決まった方の下に付く。

108b6b5 で、右辺が 1 段でない宣言（複数添字・ラベル付きフルパス）は、同名の
候補がほかにあるとき、よそのモジュールから名前で引く表へ入れないように
した。ところが見ていたのは宣言自身の右辺の形だけで、その下に 1 段の右辺で
置いた宣言は無条件で表に入った。1.3.1 ではその親が決まらなかったので、
その宣言も決まらず、候補にならなかった。実測（21000e0）:
- ACME-MIB が根を `{ iso(1) org(3) dod(6) internet(1) private(4)
  enterprises(1) 777 }`（または `{ enterprises 777 1 }`）で置き、その下に
  `system OBJECT IDENTIFIER ::= { acmeRoot 9 }` を置く。
- Z-MIB が `IMPORTS system FROM SNMPv2-MIB; zAlarm ::= { system 99 }`。
読む順 A,Z で zAlarm が 1.3.6.1.4.1.777.9.99（複数添字の根では
777.1.9.99）に付き、標準の 1.3.6.1.2.1.1.99 は 'system.99'。知らせは 0 行。
1.3.1 と 1.3.0 はどの順でも 'zAlarm'。

直し方: 1.3.1 では OID が決まらなかった宣言——自分の右辺が 1 段でない
もの、および親がそうした宣言であるもの（子孫まで）——を、同名の候補が
ほかに無いときだけ、よそのモジュールから名前で引く表へ入れる。自分の
モジュールの子の親としてはこれまでどおり使う。
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


# 1.3.1 では読めなかった根の書き方と、その根の OID
ROOT_FORMS = {
    "labeled": ("acmeRoot OBJECT IDENTIFIER ::= { iso(1) org(3) dod(6)"
                " internet(1) private(4) enterprises(1) 777 }",
                "1.3.6.1.4.1.777"),
    "multi": ("acmeRoot OBJECT IDENTIFIER ::= { enterprises 777 1 }",
              "1.3.6.1.4.1.777.1"),
}
Z_STANDARD = mib("Z-MIB DEFINITIONS ::= BEGIN",
                 "IMPORTS system FROM SNMPv2-MIB;",
                 "zAlarm OBJECT IDENTIFIER ::= { system 99 }")
ORDERS_2 = (("A.my", "Z.my"), ("Z.my", "A.my"))
ORDERS_3 = (("X.my", "Y.my", "Z.my"), ("X.my", "Z.my", "Y.my"),
            ("Y.my", "X.my", "Z.my"), ("Y.my", "Z.my", "X.my"),
            ("Z.my", "X.my", "Y.my"), ("Z.my", "Y.my", "X.my"))


class ChildOfANewlyReadParentAndImportsTest(unittest.TestCase):
    def _resolver(self, files, order):
        """exe の隣に mibs/ を作り、files を order の順で一覧させて読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-new-parent-")
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

    def test_an_imported_standard_name_is_not_taken_by_a_copy_under_a_new_root(
            self):
        """1.3.1 で読めなかった根の下に 1 段で system を置き直した MIB が
        あっても、SNMPv2-MIB の system を取り込んだ側の子が標準の OID に
        付くこと（読む順によらない）。"""
        for form, (root_decl, root_oid) in ROOT_FORMS.items():
            acme = mib("ACME-MIB DEFINITIONS ::= BEGIN",
                       root_decl,
                       "system OBJECT IDENTIFIER ::= { acmeRoot 9 }",
                       "acmeSysName OBJECT IDENTIFIER ::= { system 5 }")
            for order in ORDERS_2:
                with self.subTest(form=form, order=",".join(order)):
                    r, output = self._resolver(
                        {"A.my": acme, "Z.my": Z_STANDARD}, order)
                    self.assertEqual(r.resolve_name("zAlarm"),
                                     "1.3.6.1.2.1.1.99")
                    self.assertEqual(r.resolve_oid("1.3.6.1.2.1.1.99"),
                                     "zAlarm")
                    self.assertNotEqual(r.resolve_oid(root_oid + ".9.99"),
                                        "zAlarm",
                                        "自社の木に Z の名前が付いている")
                    # ACME 自身の子は自社の木に付く（1.3.2 の直しはそのまま）
                    self.assertEqual(r.resolve_oid(root_oid + ".9.5"),
                                     "acmeSysName")
                    self.assertEqual(self._notices(output), [])

    def test_two_links_below_a_new_root_change_nothing(self):
        """新しい形の根から 2 段下に system を置き直しても、取り込んだ側の
        子が標準の OID に付くこと（読む順によらない）。"""
        for form, (root_decl, root_oid) in ROOT_FORMS.items():
            acme = mib("ACME-MIB DEFINITIONS ::= BEGIN",
                       root_decl,
                       "acmeProducts OBJECT IDENTIFIER ::= { acmeRoot 2 }",
                       "system OBJECT IDENTIFIER ::= { acmeProducts 9 }",
                       "acmeSysName OBJECT IDENTIFIER ::= { system 5 }")
            for order in ORDERS_2:
                with self.subTest(form=form, order=",".join(order)):
                    r, output = self._resolver(
                        {"A.my": acme, "Z.my": Z_STANDARD}, order)
                    self.assertEqual(r.resolve_name("zAlarm"),
                                     "1.3.6.1.2.1.1.99")
                    self.assertEqual(r.resolve_oid(root_oid + ".2.9.5"),
                                     "acmeSysName")
                    self.assertEqual(self._notices(output), [])

    def test_an_imported_name_is_not_taken_by_a_rival_under_a_new_root(self):
        """他社が 1.3.1 で読めなかった根の下に 1 段で同名を置いても、
        取り込んだ側の子が他社の OID に付かないこと（読む順によらない）。"""
        y_mib = mib("Y-MIB DEFINITIONS ::= BEGIN",
                    "yRoot OBJECT IDENTIFIER ::= { enterprises 2222 }",
                    "foo OBJECT IDENTIFIER ::= { yRoot 5 }")
        z_mib = mib("Z-MIB DEFINITIONS ::= BEGIN",
                    "IMPORTS foo FROM Y-MIB;",
                    "zLeaf OBJECT IDENTIFIER ::= { foo 7 }")
        for form, (root_decl, root_oid) in ROOT_FORMS.items():
            x_mib = mib("X-MIB DEFINITIONS ::= BEGIN",
                        root_decl.replace("acmeRoot", "xRoot"),
                        "foo OBJECT IDENTIFIER ::= { xRoot 5 }",
                        "xLeaf OBJECT IDENTIFIER ::= { foo 3 }")
            for order in ORDERS_3:
                with self.subTest(form=form, order=",".join(order)):
                    r, output = self._resolver(
                        {"X.my": x_mib, "Y.my": y_mib, "Z.my": z_mib}, order)
                    self.assertEqual(r.resolve_name("zLeaf"),
                                     "1.3.6.1.4.1.2222.5.7")
                    self.assertNotEqual(r.resolve_oid(root_oid + ".5.7"),
                                        "zLeaf",
                                        "他社 X の OID に Z の名前が付いている")
                    # X 自身の子は、自分の foo に付く
                    self.assertEqual(r.resolve_oid(root_oid + ".5.3"),
                                     "xLeaf")
                    self.assertEqual(self._notices(output), [])

    def test_a_unique_name_under_a_new_root_is_found_by_other_modules(self):
        """同名の候補がほかに無ければ、新しい形の根の下の宣言も、取り込んだ
        モジュールの子の親になること（1.3.2 で付くようになった分を減らさない）。"""
        z_mib = mib("Z-MIB DEFINITIONS ::= BEGIN",
                    "IMPORTS acmeProducts FROM ACME-MIB;",
                    "zLeaf OBJECT IDENTIFIER ::= { acmeProducts 7 }")
        for form, (root_decl, root_oid) in ROOT_FORMS.items():
            acme = mib("ACME-MIB DEFINITIONS ::= BEGIN",
                       root_decl,
                       "acmeProducts OBJECT IDENTIFIER ::= { acmeRoot 2 }")
            for order in ORDERS_2:
                with self.subTest(form=form, order=",".join(order)):
                    r, output = self._resolver(
                        {"A.my": acme, "Z.my": z_mib}, order)
                    self.assertEqual(r.resolve_name("zLeaf"),
                                     root_oid + ".2.7")
                    self.assertEqual(self._notices(output), [])


if __name__ == "__main__":
    unittest.main()
