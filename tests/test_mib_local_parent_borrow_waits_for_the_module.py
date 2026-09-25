r"""同じモジュールの親が後の回で決まるなら、内蔵の同名を借りないことを検証する。

_resolve_definitions の docstring は「同じモジュールに親の定義があるのに
まだ解決していなければ、よその同名を使わずに次の回を待つ」と書いている
が、実装はそうなっていなかった。`parent_oid is None and
declaring.get(parent, 0) < 2` の declaring は解析した MIB モジュールの
宣言数しか数えないので、内蔵表や custom_mibs.json にある同名は「曖昧で
ない」と見なされ、1 回目の回（自分のモジュールの親がまだ未解決）で
よその OID を借りてしまう。借りて確定した子は、後の回で本当の親が
決まっても再計算されない。

実測（基準 f4cad23、ACME-MIB 1 ファイル）:
    vendorRoot MODULE-IDENTITY ::= { enterprises 65001 }
    system     OBJECT IDENTIFIER ::= { vendorRoot 1 }
    alarm      NOTIFICATION-TYPE ::= { system 99 }
_MIB_DEFINITION_PATTERNS は種類ごとに finditer するので、抽出はファイル内
の順ではなく (system, alarm, vendorRoot) の順になる。1 回目の回で system は
未解決、alarm は内蔵の system=1.3.6.1.2.1.1 を借りて確定し、結果は
    resolve_oid('1.3.6.1.4.1.65001.1.99') -> 'system.99'（本来 'alarm'）
    resolve_oid('1.3.6.1.2.1.1.99')       -> 'alarm'（標準の木に他社の名前）
になった。誤りは mib_cache.json に入るので、ファイル・custom_mibs.json・
MIB_PARSER_VERSION のどれかが変わるまで残る。

直し方: よその同名への借用を「自分のモジュールの親がもう決まらないと
分かってから」に遅らせる。通常の回は自モジュール優先だけで回し、1 回も
進まなくなった回で初めて借用を許してもう 1 回まわす。進んだら借用を
また禁じる。よそに同名の宣言があるとき諦める（利用者の決定・2026-09-20）
のは今までどおり。解決の規則が変わるので MIB_PARSER_VERSION を上げた。
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

# 抽出が (system, alarm, vendorRoot) の順になる並び。親の vendorRoot は
# MODULE-IDENTITY なので、OBJECT IDENTIFIER の節より後ろに抽出される
ACME_MIB = NL.join([
    "ACME-MIB DEFINITIONS ::= BEGIN",
    "",
    "IMPORTS",
    "    MODULE-IDENTITY, NOTIFICATION-TYPE, enterprises",
    "        FROM SNMPv2-SMI;",
    "",
    "system OBJECT IDENTIFIER ::= { vendorRoot 1 }",
    "",
    "alarm NOTIFICATION-TYPE",
    "    STATUS      current",
    '    DESCRIPTION "vendor alarm"',
    "    ::= { system 99 }",
    "",
    "vendorRoot MODULE-IDENTITY",
    '    LAST-UPDATED "202609230000Z"',
    '    ORGANIZATION "example.com"',
    '    CONTACT-INFO "noc@example.com"',
    '    DESCRIPTION  "vendor root"',
    "    ::= { enterprises 65001 }",
    "",
    "END",
    "",
])

# 対照: 自分のモジュールで親が決まらない形（internet は複数添字の右辺で
# 宣言されるので抽出から落ちる）。ここは最後に内蔵表から借りて解決する
SMI_MIB = NL.join([
    "ACME-SMI DEFINITIONS ::= BEGIN",
    "internet OBJECT IDENTIFIER ::= { iso 3 6 1 }",
    "directory OBJECT IDENTIFIER ::= { internet 1 }",
    "END",
    "",
])


class MibLocalParentBorrowWaitsForTheModuleTest(unittest.TestCase):
    def _resolver(self, files):
        """exe の隣に mibs/ を作り、files を置いて読み込む。

        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-borrow-")
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
        with contextlib.redirect_stdout(io.StringIO()):
            return MIBResolver()

    def test_a_child_waits_for_the_parent_of_its_own_module(self):
        """後の回で決まる自モジュールの親を待つこと。"""
        resolver = self._resolver({"ACME-MIB.my": ACME_MIB})

        self.assertEqual(resolver.resolve_name("vendorRoot"),
                         "1.3.6.1.4.1.65001")
        self.assertEqual(resolver.resolve_name("system"),
                         "1.3.6.1.4.1.65001.1")
        self.assertEqual(resolver.resolve_name("alarm"),
                         "1.3.6.1.4.1.65001.1.99",
                         "内蔵の同名を借りて子の OID を誤確定している")

    def test_the_vendor_oid_shows_the_vendor_name(self):
        """利用者から見える形でも、ベンダーの OID に正しい名前が出ること。"""
        resolver = self._resolver({"ACME-MIB.my": ACME_MIB})

        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.65001.1.99"),
                         "alarm", "ベンダーの Trap に名前が付かない")

    def test_the_standard_subtree_does_not_get_the_vendor_name(self):
        """標準の system 配下に、他社の名前が入らないこと。"""
        resolver = self._resolver({"ACME-MIB.my": ACME_MIB})

        shown = resolver.resolve_oid("1.3.6.1.2.1.1.99")
        self.assertNotEqual(shown, "alarm",
                            "標準 system 配下に他社の名前が入っている")
        self.assertEqual(shown, "system.99")

    def test_a_parent_that_never_resolves_locally_is_still_borrowed(self):
        """対照: 自分のモジュールで決まらない親は、最後に借りて解決すること。"""
        resolver = self._resolver({"ACME-SMI.my": SMI_MIB})

        self.assertEqual(resolver.resolve_name("directory"), "1.3.6.1.1",
                         "借用を遅らせるだけのはずが、やめてしまっている")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.1"), "directory")

    def test_both_kinds_in_one_folder_are_resolved(self):
        """同じ mibs/ に両方あっても、それぞれ正しく決まること。"""
        resolver = self._resolver({"ACME-MIB.my": ACME_MIB,
                                   "ACME-SMI.my": SMI_MIB})

        self.assertEqual(resolver.resolve_name("alarm"),
                         "1.3.6.1.4.1.65001.1.99")
        self.assertEqual(resolver.resolve_name("directory"), "1.3.6.1.1")


if __name__ == "__main__":
    unittest.main()
