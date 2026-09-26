r"""借用で決まった節をよそのモジュールが取り込んだとき、その下の 1.3.1 では読めなかった形の子が標準の OID に名前を付けないことを検証する。

借用の回: そのモジュールが宣言しているのに OID が決まらない親（右辺が
`{ mRoot sub 1 }` のように読めない）で、よそのモジュールが同名を宣言して
いなければ、内蔵表・custom_mibs.json の同名を親として借りる（1.3.1 からの
挙動）。1.3.2 の 1 周目で、借りて決まった節（子孫まで）の下に置いた新しい形
の子（複数添字・ラベル付き・TRAP-TYPE・`}` の直後）は解決しないようにした。

ところが借用の印は (モジュール, 名前) でしか持っておらず、同じモジュールの
中の子にしか効いていなかった。その節を別のモジュールが IMPORTS して子を
置くと、親は known（名前で引く表）から引かれ、借用の印が付かない。
実測（ebbe593）: M-MIB が `system ::= { mRoot sub 1 }`（読めない）と
`acmeX ::= { system 4 }`（1.3.1 と同じく借用で 1.3.6.1.2.1.1.4）を置き、
N-MIB が `IMPORTS acmeX FROM M-MIB;` のうえで `{ acmeX 6 1 }`・
`{ acmeX nl(7) }`・`TRAP-TYPE ENTERPRISE acmeX ::= 3`・`}` の直後の
`{ acmeX 8 }` を置くと、標準 sysContact の下の 1.3.6.1.2.1.1.4.6.1 / .4.7 /
.4.0.3 / .4.8 に他社の名前が付いた（知らせは 0 行）。1.3.1 はどれも抜き出さ
ないので名前なし。同じ子を M-MIB の中に置けば ebbe593 でも名前なし。

直し方: known の値が借用で決まった宣言から来た名前を、fresh_known と同じ
作りで borrowed_known に覚える。取り込んだ親（known から引く親）がそこに
あれば借用の下として扱い、新しい形の子は解決しない（利用者の決定
2026-09-20『確定できないときは名前を付けない』）。1.3.1 と同じ `{ 親 番号 }`
の子は、これまでどおり借用した節の下に付く。
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


M_MIB = mib("M-MIB DEFINITIONS ::= BEGIN",
            "IMPORTS enterprises FROM SNMPv2-SMI;",
            "mRoot OBJECT IDENTIFIER ::= { enterprises 65020 }",
            "system OBJECT IDENTIFIER ::= { mRoot sub 1 }",
            "acmeX OBJECT IDENTIFIER ::= { system 4 }")
N_MIB = mib("N-MIB DEFINITIONS ::= BEGIN",
            "IMPORTS acmeX FROM M-MIB;",
            "nRoot OBJECT IDENTIFIER ::= { enterprises 65021 }",
            "nChild OBJECT IDENTIFIER ::= { acmeX 6 1 }",
            "nLabeled OBJECT IDENTIFIER ::= { acmeX nl(7) }",
            "nTrap TRAP-TYPE",
            "    ENTERPRISE acmeX",
            "    ::= 3",
            "nOne OBJECT IDENTIFIER ::= { nRoot 1 } nBrace OBJECT IDENTIFIER"
            " ::= { acmeX 8 }",
            "nSingle OBJECT IDENTIFIER ::= { acmeX 9 }",
            "nSingleLeaf OBJECT IDENTIFIER ::= { nSingle 1 }",
            "nUnderSingle OBJECT IDENTIFIER ::= { nSingle 2 1 }")
ORDERS = (("M.my", "N.my"), ("N.my", "M.my"))


class NewFormsUnderAnImportedBorrowedNodeTest(unittest.TestCase):
    def _resolver(self, files, order):
        """exe の隣に mibs/ を作り、files を order の順で一覧させて読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-imported-borrowed-")
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

    def test_new_forms_under_an_imported_borrowed_node_stay_unnamed(self):
        """借用で決まった節を取り込んだモジュールの新しい形の子（と、借用の
        下の 1 段の子のさらに下の新しい形）は、標準の OID に名前を付けない
        こと（読む順によらない）。"""
        cases = (("nChild", "1.3.6.1.2.1.1.4.6.1"),
                 ("nLabeled", "1.3.6.1.2.1.1.4.7"),
                 ("nTrap", "1.3.6.1.2.1.1.4.0.3"),
                 ("nBrace", "1.3.6.1.2.1.1.4.8"),
                 ("nUnderSingle", "1.3.6.1.2.1.1.4.9.2.1"))
        for order in ORDERS:
            r, _ = self._resolver({"M.my": M_MIB, "N.my": N_MIB}, order)
            for name, standard_oid in cases:
                with self.subTest(order=",".join(order), name=name):
                    self.assertNotEqual(r.resolve_oid(standard_oid), name,
                                        "標準の OID に他社の名前が付いている")
                    self.assertIsNone(r.resolve_name(name))

    def test_single_steps_under_an_imported_borrowed_node_are_unchanged(self):
        """1.3.1 と同じ `{ 親 番号 }` の子は、取り込んだモジュールの側でも
        これまでどおり借用した節の下に付くこと（読む順によらない）。"""
        for order in ORDERS:
            with self.subTest(order=",".join(order)):
                r, _ = self._resolver({"M.my": M_MIB, "N.my": N_MIB}, order)
                self.assertEqual(r.resolve_name("acmeX"), "1.3.6.1.2.1.1.4")
                self.assertEqual(r.resolve_name("nSingle"),
                                 "1.3.6.1.2.1.1.4.9")
                self.assertEqual(r.resolve_name("nSingleLeaf"),
                                 "1.3.6.1.2.1.1.4.9.1")
                self.assertEqual(r.resolve_name("nOne"),
                                 "1.3.6.1.4.1.65021.1")

    def test_new_forms_under_an_imported_unborrowed_node_still_resolve(self):
        """取り込んだ節が借用で決まったものでなければ、その下の新しい形の子
        はこれまでどおり名前が付くこと。"""
        m_mib = mib("M-MIB DEFINITIONS ::= BEGIN",
                    "IMPORTS enterprises FROM SNMPv2-SMI;",
                    "mRoot OBJECT IDENTIFIER ::= { enterprises 65020 }",
                    "acmeX OBJECT IDENTIFIER ::= { mRoot 4 }")
        for order in ORDERS:
            with self.subTest(order=",".join(order)):
                r, _ = self._resolver({"M.my": m_mib, "N.my": N_MIB}, order)
                self.assertEqual(r.resolve_name("nChild"),
                                 "1.3.6.1.4.1.65020.4.6.1")
                self.assertEqual(r.resolve_name("nLabeled"),
                                 "1.3.6.1.4.1.65020.4.7")
                self.assertEqual(r.resolve_oid("1.3.6.1.4.1.65020.4.0.3"),
                                 "nTrap")
                self.assertEqual(r.resolve_name("nBrace"),
                                 "1.3.6.1.4.1.65020.4.8")


if __name__ == "__main__":
    unittest.main()
