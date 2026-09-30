r"""1.3.2 から読めるようになった右辺の宣言が、IMPORTS の既知の制限を広げないことを検証する。

既知の制限: IMPORTS を見ないので、2 つの候補（2 つのモジュールの同名、
または内蔵表・custom_mibs.json の名前とモジュールの同名）があるとき、
その名前を取り込んだ第三のモジュールの子は、後に決まった方の下に付く。

1.3.2 で右辺を全部読むようにした（複数添字の `{ xRoot 5 1 }`、ラベル付き
フルパスの `{ iso(1) … 1111 5 }`）ので、1.3.1 まで見えていなかった宣言が
この候補に加わった。実測（0a2054d）:
- X-MIB が foo を `{ xRoot 5 1 }` で、Y-MIB が foo を `{ yRoot 5 }` で宣言し、
  Z-MIB が `IMPORTS foo FROM Y-MIB; zLeaf ::= { foo 7 }` を置く。読む順
  Y,X,Z で zLeaf が他社 X の 1.3.6.1.4.1.1111.5.1.7 に付き、知らせは 0 行
  （ラベル付きでは 1111.5.7）。1.3.1 と 1.3.0 はどの順でも正しい
  1.3.6.1.4.1.2222.5.7。
- 自社の木に system をラベル付きか複数添字で置いた ACME-MIB と、
  `IMPORTS system FROM SNMPv2-MIB; zAlarm ::= { system 99 }` の Z-MIB を
  X,Z の順で読むと、zAlarm が 1.3.6.1.4.1.777.9.99（または 777.9.1.99）に
  なり、標準の 1.3.6.1.2.1.1.99 は 'system.99'。1.3.1 と 1.3.0 は 'zAlarm'。
- 1.3.2 から抜き出す SMIv1 の TRAP-TYPE も同じ。他社の `linkDown TRAP-TYPE
  ENTERPRISE xRoot ::= 3` があると、内蔵の linkDown を親にした zLeaf が
  読む順 X,Z で 1.3.6.1.4.1.1111.0.3.1 に付いた（1.3.1 は内蔵の
  1.3.6.1.6.3.1.1.5.3.1）。

直し方: そうした宣言（右辺が `{ 親 添字 }` の 1 段でないもの）は、自分の
モジュールの子の親としてはこれまでどおり使うが、同じ名前の候補がほかに
あるときは、よそのモジュールから名前で引く表には入れない。候補がほかに
無い名前は入れる（1.3.2 で名前が付くようになった分を減らさない）。
その表に入れなかった宣言も、よそのモジュールの親を諦めたときの知らせでは
「同じ名前が別のモジュールにもある」側として数える（0a2054d と同じく 1 行
出す。数えないと、決まらない親の子が知らせ無しで名前なしになる）。
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


# X-MIB の foo の書き方（1.3.1 までは読めなかった形）
FOO_FORMS = {
    "multi": ("foo OBJECT IDENTIFIER ::= { xRoot 5 1 }",
              "1.3.6.1.4.1.1111.5.1"),
    "labeled": ("foo OBJECT IDENTIFIER ::= { iso(1) org(3) dod(6)"
                " internet(1) private(4) enterprises(1) 1111 5 }",
                "1.3.6.1.4.1.1111.5"),
}
Y_MIB = mib("Y-MIB DEFINITIONS ::= BEGIN",
            "yRoot OBJECT IDENTIFIER ::= { enterprises 2222 }",
            "foo OBJECT IDENTIFIER ::= { yRoot 5 }")
Z_MIB = mib("Z-MIB DEFINITIONS ::= BEGIN",
            "IMPORTS foo FROM Y-MIB;",
            "zLeaf OBJECT IDENTIFIER ::= { foo 7 }")

# 自社の木に標準名 system を置き直した MIB の書き方
SYSTEM_FORMS = {
    "labeled": ("system OBJECT IDENTIFIER ::= { iso(1) org(3) dod(6)"
                " internet(1) private(4) enterprises(1) 777 9 }",
                "1.3.6.1.4.1.777.9"),
    "multi": ("system OBJECT IDENTIFIER ::= { acmeRoot 9 1 }",
              "1.3.6.1.4.1.777.9.1"),
}
Z_STANDARD = mib("Z-MIB DEFINITIONS ::= BEGIN",
                 "IMPORTS system FROM SNMPv2-MIB;",
                 "zAlarm OBJECT IDENTIFIER ::= { system 99 }")

ORDERS_3 = (("X.my", "Y.my", "Z.my"), ("X.my", "Z.my", "Y.my"),
            ("Y.my", "X.my", "Z.my"), ("Y.my", "Z.my", "X.my"),
            ("Z.my", "X.my", "Y.my"), ("Z.my", "Y.my", "X.my"))
ORDERS_2 = (("X.my", "Z.my"), ("Z.my", "X.my"))

# 内蔵表にある Trap 名を、他社が SMIv1 の TRAP-TYPE で宣言する
X_TRAP = mib("X-MIB DEFINITIONS ::= BEGIN",
             "xRoot OBJECT IDENTIFIER ::= { enterprises 1111 }",
             "linkDown TRAP-TYPE",
             "    ENTERPRISE xRoot",
             "    ::= 3")
Z_TRAP = mib("Z-MIB DEFINITIONS ::= BEGIN",
             "zLeaf OBJECT IDENTIFIER ::= { linkDown 1 }")

# 本当に曖昧な形（名前を付けずに知らせる側）。A-MIB の shared は取り込み元
# （aMissing）が無くて決まらず、B-MIB も shared を宣言している
A_AMBIGUOUS = mib("A-MIB DEFINITIONS ::= BEGIN",
                  "IMPORTS aMissing FROM A-SMI;",
                  "shared OBJECT IDENTIFIER ::= { aMissing 1 }",
                  "aAlarm OBJECT IDENTIFIER ::= { shared 1 }")
# B-MIB の shared の書き方（1.3.1 までは読めなかった形）
SHARED_FORMS = {
    "multi": ("shared OBJECT IDENTIFIER ::= { bRoot 1 2 }",
              "1.3.6.1.4.1.2222.1.2"),
    "labeled": ("shared OBJECT IDENTIFIER ::= { iso(1) org(3) dod(6)"
                " internet(1) private(4) enterprises(1) 2222 3 }",
                "1.3.6.1.4.1.2222.3"),
}


class MultiNumberValueAndImportsTest(unittest.TestCase):
    def _resolver(self, files, order):
        """exe の隣に mibs/ を作り、files を order の順で一覧させて読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-imports-")
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

    def test_an_imported_name_is_not_taken_by_a_multi_number_rival(self):
        """他社が複数添字・ラベル付きで同名を宣言しても、取り込んだ側の子が
        他社の OID に付かないこと（読む順によらない）。"""
        for form, (decl, foo_oid) in FOO_FORMS.items():
            x_mib = mib("X-MIB DEFINITIONS ::= BEGIN",
                        "xRoot OBJECT IDENTIFIER ::= { enterprises 1111 }",
                        decl,
                        "xLeaf OBJECT IDENTIFIER ::= { foo 3 }")
            for order in ORDERS_3:
                with self.subTest(form=form, order=",".join(order)):
                    r, output = self._resolver(
                        {"X.my": x_mib, "Y.my": Y_MIB, "Z.my": Z_MIB}, order)
                    self.assertEqual(r.resolve_name("zLeaf"),
                                     "1.3.6.1.4.1.2222.5.7")
                    self.assertNotEqual(r.resolve_oid(foo_oid + ".7"),
                                        "zLeaf",
                                        "他社 X の OID に Z の名前が付いている")
                    # X 自身の子は、読めるようになった自分の foo に付く
                    self.assertEqual(r.resolve_oid(foo_oid + ".3"), "xLeaf")
                    self.assertEqual(self._notices(output), [])

    def test_an_imported_standard_name_is_not_taken_by_a_vendor_copy(self):
        """自社の木に system を置き直した MIB があっても、SNMPv2-MIB の
        system を取り込んだ側の子が標準の OID に付くこと（読む順によらない）。"""
        for form, (decl, system_oid) in SYSTEM_FORMS.items():
            acme = mib("ACME-MIB DEFINITIONS ::= BEGIN",
                       "acmeRoot OBJECT IDENTIFIER ::= { enterprises 777 }",
                       decl,
                       "acmeSysName OBJECT IDENTIFIER ::= { system 5 }")
            for order in ORDERS_2:
                with self.subTest(form=form, order=",".join(order)):
                    r, output = self._resolver(
                        {"X.my": acme, "Z.my": Z_STANDARD}, order)
                    self.assertEqual(r.resolve_name("zAlarm"),
                                     "1.3.6.1.2.1.1.99")
                    self.assertEqual(r.resolve_oid("1.3.6.1.2.1.1.99"),
                                     "zAlarm")
                    self.assertNotEqual(r.resolve_oid(system_oid + ".99"),
                                        "zAlarm",
                                        "自社の木に Z の名前が付いている")
                    # ACME 自身の子は自社の木に付く（1.3.2 の直しはそのまま）
                    self.assertEqual(r.resolve_oid(system_oid + ".5"),
                                     "acmeSysName")
                    self.assertNotEqual(r.resolve_oid("1.3.6.1.2.1.1.5"),
                                        "acmeSysName")
                    self.assertEqual(self._notices(output), [])

    def test_a_multi_number_name_without_a_rival_is_found_by_other_modules(
            self):
        """同名の候補がほかに無ければ、複数添字・ラベル付きの宣言も、
        取り込んだモジュールの子の親になること。"""
        for form, (decl, foo_oid) in FOO_FORMS.items():
            x_mib = mib("X-MIB DEFINITIONS ::= BEGIN",
                        "xRoot OBJECT IDENTIFIER ::= { enterprises 1111 }",
                        decl)
            z_mib = mib("Z-MIB DEFINITIONS ::= BEGIN",
                        "IMPORTS foo FROM X-MIB;",
                        "zLeaf OBJECT IDENTIFIER ::= { foo 7 }")
            for order in ORDERS_2:
                with self.subTest(form=form, order=",".join(order)):
                    r, output = self._resolver(
                        {"X.my": x_mib, "Z.my": z_mib}, order)
                    self.assertEqual(r.resolve_name("zLeaf"), foo_oid + ".7")
                    self.assertEqual(self._notices(output), [])

    def test_a_trap_type_does_not_take_a_builtin_name_from_other_modules(self):
        """他社が内蔵表と同じ名前の TRAP-TYPE を宣言しても、その名前を親に
        使うよそのモジュールの子が、他社の Trap の下に付かないこと（読む順に
        よらない）。他社の Trap 自身には名前が付く。"""
        for order in ORDERS_2:
            with self.subTest(order=",".join(order)):
                r, output = self._resolver({"X.my": X_TRAP, "Z.my": Z_TRAP},
                                           order)
                self.assertEqual(r.resolve_name("zLeaf"),
                                 "1.3.6.1.6.3.1.1.5.3.1")
                self.assertEqual(r.resolve_oid("1.3.6.1.6.3.1.1.5.3.1"),
                                 "zLeaf")
                self.assertNotEqual(r.resolve_oid("1.3.6.1.4.1.1111.0.3.1"),
                                    "zLeaf",
                                    "他社の Trap の下に Z の名前が付いている")
                self.assertEqual(r.resolve_oid("1.3.6.1.4.1.1111.0.3"),
                                 "linkDown")
                self.assertEqual(self._notices(output), [])

    def test_giving_up_is_still_reported_when_the_rival_has_a_new_form(self):
        """よそのモジュールの同名が複数添字・ラベル付きでも、親を決められずに
        諦めたことを 1 行知らせ、よその木に名前を付けないこと。"""
        for form, (decl, shared_oid) in SHARED_FORMS.items():
            b_mib = mib("B-MIB DEFINITIONS ::= BEGIN",
                        "bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }",
                        decl)
            for order in (("A.my", "B.my"), ("B.my", "A.my")):
                with self.subTest(form=form, order=",".join(order)):
                    r, output = self._resolver(
                        {"A.my": A_AMBIGUOUS, "B.my": b_mib}, order)
                    self.assertIsNone(r.resolve_name("aAlarm"))
                    self.assertNotEqual(r.resolve_oid(shared_oid + ".1"),
                                        "aAlarm",
                                        "B の OID に A-MIB の名前が付いている")
                    self.assertEqual(len(self._notices(output)), 1,
                                     "諦めたことの知らせが 1 行ではない: %r"
                                     % (output.splitlines(),))


if __name__ == "__main__":
    unittest.main()
