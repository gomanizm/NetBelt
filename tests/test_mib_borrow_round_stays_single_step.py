r"""借用の回で同名を借りる子が、1.3.1 と同じ 1 段の子だけであることを検証する。

借用の回: そのモジュールが宣言している（_MIB_LOCAL_NAME で拾える）のに
OID が決まらない親（右辺が `{ acmeRoot acmeSub 9 }` のように読めない）
で、よそのモジュールが同名を宣言していなければ、内蔵表・custom_mibs.json
の同名を親として借りる（1.3.1 からの挙動）。

1.3.2 から複数添字の子（`{ system 5 1 }`）と SMIv1 の TRAP-TYPE（ENTERPRISE
の下の 0.N）も抜き出すので、その子もこの借用に乗った。実測（0a2054d）:
自社の MIB が system を読めない右辺で宣言し、acmeMulti ::= { system 5 1 }
と acmeTrap TRAP-TYPE ENTERPRISE system ::= 3 を置くと、標準の
1.3.6.1.2.1.1.5.1 に 'acmeMulti'、1.3.6.1.2.1.1.0.3 に 'acmeTrap' が付いた
（標準の OID に自社の名前）。1.3.1 はどちらも抜き出さないので名前なし。

直し方: 借用は 1 段の子（{ 親 添字 }）だけに許す。複数添字と TRAP-TYPE の
子は、親が自分のモジュールで決まらなければ名前を付けない（利用者の決定
2026-09-20『確定できないときは名前を付けない』）。親を宣言していない
モジュール（IMPORTS 相当）の子は借用ではないので、これまでどおり付く。
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


# 自社の MIB が標準名 system を読めない右辺で宣言する
ACME_MIB = mib("ACME-MIB DEFINITIONS ::= BEGIN",
               "acmeRoot OBJECT IDENTIFIER ::= { enterprises 777 }",
               "system OBJECT IDENTIFIER ::= { acmeRoot acmeSub 9 }",
               "acmeMulti OBJECT IDENTIFIER ::= { system 5 1 }",
               "acmeTrap TRAP-TYPE",
               "    ENTERPRISE system",
               "    ::= 3")


class MibBorrowRoundStaysSingleStepTest(unittest.TestCase):
    def _resolver(self, files):
        """exe の隣に mibs/ を作り、files を置いて読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-borrow-step-")
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

    def test_a_multi_number_child_does_not_borrow_a_standard_parent(self):
        """親が自分のモジュールで決まらない複数添字の子は、標準の同名の
        下に付かないこと。"""
        r, _ = self._resolver({"ACME.my": ACME_MIB})
        self.assertNotEqual(r.resolve_oid("1.3.6.1.2.1.1.5.1"), "acmeMulti",
                            "標準の OID に自社の名前が付いている")
        self.assertIsNone(r.resolve_name("acmeMulti"))

    def test_a_trap_type_does_not_borrow_a_standard_enterprise(self):
        """ENTERPRISE が自分のモジュールで決まらない TRAP-TYPE は、標準の
        同名の下に付かないこと。"""
        r, _ = self._resolver({"ACME.my": ACME_MIB})
        self.assertNotEqual(r.resolve_oid("1.3.6.1.2.1.1.0.3"), "acmeTrap",
                            "標準の OID に自社の Trap 名が付いている")
        self.assertIsNone(r.resolve_name("acmeTrap"))

    def test_an_imported_parent_still_names_a_multi_number_child(self):
        """親を宣言していないモジュール（IMPORTS 相当）の複数添字の子と
        TRAP-TYPE は、借用ではないので、これまでどおり内蔵の親の下に付くこと。"""
        z_mib = mib("Z-MIB DEFINITIONS ::= BEGIN",
                    "IMPORTS system FROM SNMPv2-MIB;",
                    "zMulti OBJECT IDENTIFIER ::= { system 9 1 }",
                    "zTrap TRAP-TYPE",
                    "    ENTERPRISE system",
                    "    ::= 4")
        r, _ = self._resolver({"Z.my": z_mib})
        self.assertEqual(r.resolve_name("zMulti"), "1.3.6.1.2.1.1.9.1")
        self.assertEqual(r.resolve_oid("1.3.6.1.2.1.1.0.4"), "zTrap")


if __name__ == "__main__":
    unittest.main()
