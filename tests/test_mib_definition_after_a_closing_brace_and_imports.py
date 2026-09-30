r"""`}` の直後から始まる定義が、IMPORTS の既知の制限と借用を 1.3.1 より広げないことを検証する。

0a2054d で、前の定義を閉じる `}` と同じ行から始まる定義（実 MIB の
DS3-MIB.my の ds3Compliances）に名前が付くよう戻した（1.3.1 の行頭の錨で
落ちていた）。ただし 1.3.1 はこの定義を抜き出さなかったので、解決の側で
1.3.1 の定義と同じに扱うと、1.3.1 に無かった候補と借用が増える。実測
（21000e0）:
- IMPORTS の候補: BX-MIB が 1 行に `bxRoot OBJECT IDENTIFIER ::= { enterprises
  888 } interfaces OBJECT IDENTIFIER ::= { bxRoot 2 }` と書き、BZ-MIB が
  `IMPORTS interfaces FROM IF-MIB; bzLeaf ::= { interfaces 50 }` を置く。
  読む順 BX,BZ で bzLeaf が 1.3.6.1.4.1.888.2.50 に付き、標準の
  1.3.6.1.2.1.2.50 は 'interfaces.50'（知らせは 0 行）。1.3.1 はどの順でも
  'bzLeaf'（v1.3.0 は 21000e0 と同じ）。
- 借用の回: 自社の MIB が system を読めない右辺で宣言し、`}` の直後に
  `acmeAfter OBJECT IDENTIFIER ::= { system 5 }` を置くと、標準の
  1.3.6.1.2.1.1.5 に 'acmeAfter' が付いた。1.3.1 は抜き出さないので名前なし。

直し方: `}` の直後から抜き出した定義は、複数添字・ラベル付きと同じく
1.3.1 では読めなかった形として扱う（借用に乗せない。同名の候補がほかに
あれば、よそのモジュールから名前で引く表へ入れない）。その定義自身と、
自分のモジュールの子には、0a2054d のとおり名前が付く。
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


ORDERS_2 = (("BX.my", "BZ.my"), ("BZ.my", "BX.my"))


class DefinitionAfterAClosingBraceAndImportsTest(unittest.TestCase):
    def _resolver(self, files, order=None):
        """exe の隣に mibs/ を作り、files を order の順で一覧させて読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-brace-imports-")
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

    def test_an_imported_standard_name_is_not_taken_by_a_copy_after_a_brace(
            self):
        """`}` の直後に interfaces を置き直した MIB があっても、IF-MIB の
        interfaces を取り込んだ側の子が標準の OID に付くこと（読む順に
        よらない）。置き直した interfaces 自身とその子には名前が付く。"""
        bx = mib("BX-MIB DEFINITIONS ::= BEGIN",
                 "bxRoot OBJECT IDENTIFIER ::= { enterprises 888 }"
                 " interfaces OBJECT IDENTIFIER ::= { bxRoot 2 }",
                 "bxIfLeaf OBJECT IDENTIFIER ::= { interfaces 7 }")
        bz = mib("BZ-MIB DEFINITIONS ::= BEGIN",
                 "IMPORTS interfaces FROM IF-MIB;",
                 "bzLeaf OBJECT IDENTIFIER ::= { interfaces 50 }")
        for order in ORDERS_2:
            with self.subTest(order=",".join(order)):
                r, output = self._resolver({"BX.my": bx, "BZ.my": bz}, order)
                self.assertEqual(r.resolve_name("bzLeaf"), "1.3.6.1.2.1.2.50")
                self.assertEqual(r.resolve_oid("1.3.6.1.2.1.2.50"), "bzLeaf")
                self.assertNotEqual(r.resolve_oid("1.3.6.1.4.1.888.2.50"),
                                    "bzLeaf",
                                    "他社 BX の木に BZ の名前が付いている")
                # 0a2054d で戻した名前はそのまま
                self.assertEqual(r.resolve_oid("1.3.6.1.4.1.888.2"),
                                 "interfaces")
                self.assertEqual(r.resolve_oid("1.3.6.1.4.1.888.2.7"),
                                 "bxIfLeaf")
                self.assertEqual(self._notices(output), [])

    def test_a_child_after_a_brace_does_not_borrow_a_standard_parent(self):
        """親が自分のモジュールで決まらない `}` の直後の子は、標準の同名の
        下に付かないこと。行頭から始まる 1 段の子の借用は変えない。"""
        acme = mib("ACME-MIB DEFINITIONS ::= BEGIN",
                   "acmeRoot OBJECT IDENTIFIER ::= { enterprises 777 }",
                   "system OBJECT IDENTIFIER ::= { acmeRoot acmeSub 9 }",
                   "acmeSingle OBJECT IDENTIFIER ::= { system 4 }"
                   " acmeAfter OBJECT IDENTIFIER ::= { system 5 }")
        r, _ = self._resolver({"ACME.my": acme})
        self.assertNotEqual(r.resolve_oid("1.3.6.1.2.1.1.5"), "acmeAfter",
                            "標準の OID に自社の名前が付いている")
        self.assertIsNone(r.resolve_name("acmeAfter"))
        # 対照: 1.3.1 から抜き出せた行頭の子は、これまでどおり借用する
        self.assertEqual(r.resolve_name("acmeSingle"), "1.3.6.1.2.1.1.4")

    def test_a_unique_name_after_a_brace_is_found_by_other_modules(self):
        """同名の候補がほかに無ければ、`}` の直後の定義も、取り込んだ
        モジュールの子の親になること。"""
        bx = mib("BX-MIB DEFINITIONS ::= BEGIN",
                 "bxRoot OBJECT IDENTIFIER ::= { enterprises 888 }"
                 " bxThing OBJECT IDENTIFIER ::= { bxRoot 3 }")
        bz = mib("BZ-MIB DEFINITIONS ::= BEGIN",
                 "IMPORTS bxThing FROM BX-MIB;",
                 "bzLeaf OBJECT IDENTIFIER ::= { bxThing 7 }")
        for order in ORDERS_2:
            with self.subTest(order=",".join(order)):
                r, output = self._resolver({"BX.my": bx, "BZ.my": bz}, order)
                self.assertEqual(r.resolve_name("bzLeaf"),
                                 "1.3.6.1.4.1.888.3.7")
                self.assertEqual(self._notices(output), [])


if __name__ == "__main__":
    unittest.main()
