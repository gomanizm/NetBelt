r"""借用で決まった節の下に置いた 1.3.1 では読めなかった形の子が、標準の OID に名前を付けないことを検証する。

借用の回: そのモジュールが宣言しているのに OID が決まらない親（右辺が
`{ acmeRoot acmeSub 9 }` のように読めない）で、よそのモジュールが同名を
宣言していなければ、内蔵表・custom_mibs.json の同名を親として借りる
（1.3.1 からの挙動）。21000e0 と 4e907de で、借りてよいのは 1.3.1 と同じ
`{ 親 番号 }` の子だけにした（複数添字・ラベル付き・TRAP-TYPE の子は名前を
付けない）。

ところが見ていたのは借りる子自身だけで、借りて決まった子のさらに下に
置いた新しい形の子は、自分のモジュールの親として普通に解決していた。
実測（068a901）: 自社の MIB が system を読めない右辺で宣言し、
`acmeSingle ::= { system 4 }`（1.3.1 と同じく借用で 1.3.6.1.2.1.1.4）の下に
`{ acmeSingle 6 1 }`・`{ acmeMid lab(7) }`・`TRAP-TYPE ENTERPRISE acmeSingle
::= 2`・`}` の直後の `{ acmeMid 8 }` を置くと、標準の 1.3.6.1.2.1.1.4.6.1 /
.4.3.7 / .4.0.2 / .4.3.8 に自社の名前が付いた（知らせは 0 行）。1.3.1 は
どれも抜き出さないので名前なし。

直し方: 借用で決まった宣言とその子孫を覚えておき、その下の 1.3.1 では
読めなかった形の子は解決しない（利用者の決定 2026-09-20『確定できないとき
は名前を付けない』）。1.3.1 と同じ `{ 親 番号 }` の子は、これまでどおり
借用した節の下に付く。
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


ACME_MIB = mib("ACME-MIB DEFINITIONS ::= BEGIN",
               "acmeRoot OBJECT IDENTIFIER ::= { enterprises 777 }",
               "system OBJECT IDENTIFIER ::= { acmeRoot acmeSub 9 }",
               "acmeSingle OBJECT IDENTIFIER ::= { system 4 }",
               "acmeMid OBJECT IDENTIFIER ::= { acmeSingle 3 }",
               "acmeDeepMulti OBJECT IDENTIFIER ::= { acmeSingle 6 1 }",
               "acmeDeepLabeled OBJECT IDENTIFIER ::= { acmeMid lab(7) }",
               "acmeDeepTrap TRAP-TYPE",
               "    ENTERPRISE acmeSingle",
               "    ::= 2",
               "acmeLeaf OBJECT IDENTIFIER ::= { acmeMid 1 }"
               " acmeDeepAfter OBJECT IDENTIFIER ::= { acmeMid 8 }",
               "acmeUnder OBJECT IDENTIFIER ::= { acmeDeepMulti 5 }")


class NewFormsUnderABorrowedParentTest(unittest.TestCase):
    def _resolver(self, files):
        """exe の隣に mibs/ を作り、files を置いて読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-borrowed-chain-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        mibs = os.path.join(exe_dir, "mibs")
        os.makedirs(mibs)
        for name, text in files.items():
            with open(os.path.join(mibs, name), "wb") as f:
                f.write(text.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resolver = MIBResolver()
        return resolver, out.getvalue()

    def test_new_forms_under_a_borrowed_node_stay_unnamed(self):
        """借用で決まった節の下の新しい形の子（とその子）は、標準の OID に
        名前を付けないこと。"""
        r, _ = self._resolver({"ACME.my": ACME_MIB})
        cases = (("acmeDeepMulti", "1.3.6.1.2.1.1.4.6.1"),
                 ("acmeDeepLabeled", "1.3.6.1.2.1.1.4.3.7"),
                 ("acmeDeepTrap", "1.3.6.1.2.1.1.4.0.2"),
                 ("acmeDeepAfter", "1.3.6.1.2.1.1.4.3.8"),
                 ("acmeUnder", "1.3.6.1.2.1.1.4.6.1.5"))
        for name, standard_oid in cases:
            with self.subTest(name=name):
                self.assertNotEqual(r.resolve_oid(standard_oid), name,
                                    "標準の OID に自社の名前が付いている")
                self.assertIsNone(r.resolve_name(name))

    def test_single_steps_under_a_borrowed_node_are_unchanged(self):
        """1.3.1 と同じ `{ 親 番号 }` の子は、これまでどおり借用した節の
        下に付くこと。"""
        r, _ = self._resolver({"ACME.my": ACME_MIB})
        self.assertEqual(r.resolve_name("acmeSingle"), "1.3.6.1.2.1.1.4")
        self.assertEqual(r.resolve_name("acmeMid"), "1.3.6.1.2.1.1.4.3")
        self.assertEqual(r.resolve_name("acmeLeaf"), "1.3.6.1.2.1.1.4.3.1")

    def test_new_forms_under_an_imported_parent_still_resolve(self):
        """親を宣言していないモジュール（IMPORTS 相当）の子の下の新しい形は、
        借用ではないので、これまでどおり名前が付くこと。"""
        z_mib = mib("Z-MIB DEFINITIONS ::= BEGIN",
                    "IMPORTS system FROM SNMPv2-MIB;",
                    "zMid OBJECT IDENTIFIER ::= { system 7 }",
                    "zDeep OBJECT IDENTIFIER ::= { zMid 6 1 }",
                    "zTrap TRAP-TYPE",
                    "    ENTERPRISE zMid",
                    "    ::= 3")
        r, _ = self._resolver({"Z.my": z_mib})
        self.assertEqual(r.resolve_name("zDeep"), "1.3.6.1.2.1.1.7.6.1")
        self.assertEqual(r.resolve_oid("1.3.6.1.2.1.1.7.0.3"), "zTrap")


if __name__ == "__main__":
    unittest.main()
