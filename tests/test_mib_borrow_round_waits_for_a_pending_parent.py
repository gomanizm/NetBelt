r"""借用を許した回の中でも、同じモジュールの親を待つことを検証する。

5730ab3「同じモジュールの親が決まるまで借用を遅らせる」は、1 件も進まない
回が来たところで borrow=True にしてもう 1 回まわす。ところがその 1 回は
pending をリストの順に 1 パスするだけなので、パスの前の方にある子が、
同じパスの後ろで決まるはずの自モジュールの親を待たずに、内蔵表の同名を
借りて確定してしまう。深さ 2 以上の鎖の根が借用待ちのときに起きる。

実測（基準 aa38a2b、mibs/ に ACME2-MIB.my 1 枚）:
    mib-2      OBJECT IDENTIFIER ::= { iso(1) org(3) ... mgmt(2) 1 }
    vendorRoot MODULE-IDENTITY   ::= { mib-2 9999 }
    system     OBJECT IDENTIFIER ::= { vendorRoot 1 }
    alarm      NOTIFICATION-TYPE ::= { system 99 }
mib-2 はラベル付きフルパスの右辺なので抽出から落ち、_MIB_LOCAL_NAME の
宣言だけが残る＝借用が要る親になる。抽出の順は (system, alarm, vendorRoot)
で、1 回目の回は 1 件も進まない。続く借用の回で alarm が system より先に
来るため、system がまだ未解決のまま内蔵の system=1.3.6.1.2.1.1 を借りて
確定した:
    vendorRoot = 1.3.6.1.2.1.9999
    system     = 1.3.6.1.2.1.9999.1
    alarm      = 1.3.6.1.2.1.1.99       （本来 1.3.6.1.2.1.9999.1.99）
    resolve_oid('1.3.6.1.2.1.9999.1.99') -> 'system.99'
    resolve_oid('1.3.6.1.2.1.1.99')      -> 'alarm'（標準の木に他社の名前）
誤りは mib_cache.json に入るので、ファイル・custom_mibs.json・
MIB_PARSER_VERSION のどれかが変わるまで残る。

直し方: 借用を許す回でも、その回に残っている定義が親になるものは借りない。
回の初めに「この回に残っている (モジュール, 名前)」を作り、親がその中に
あるうちは待つ。鎖の根（残りの定義が親にならない側）だけが借り、進んだら
借用をまた禁じるので、残りは次の回に自分のモジュールの親で決まる。同じ
パスの中で先に親が決まった子はそのまま解決するので、回数は増えない。
自分のモジュールでは決して決まらない親を最後に借りる対照は変わらない。
解決の規則が変わるので MIB_PARSER_VERSION を上げた。
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

# 抽出が (system, alarm, vendorRoot) の順になる並び。鎖の根 vendorRoot の
# 親 mib-2 はラベル付きフルパスなので抽出から落ち、宣言だけが残る
ACME2_MIB = NL.join([
    "ACME2-MIB DEFINITIONS ::= BEGIN",
    "",
    "IMPORTS",
    "    MODULE-IDENTITY, NOTIFICATION-TYPE",
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
    "    ::= { mib-2 9999 }",
    "",
    "mib-2 OBJECT IDENTIFIER ::= { iso(1) org(3) dod(6) internet(1) "
    "mgmt(2) 1 }",
    "",
    "END",
    "",
])

# 上と同じ鎖を _resolve_definitions へ直接渡す形。並び順だけを変えて
# 結果が動かないことを見る
CHAIN = (
    ("system", "vendorRoot", "1", "ACME2-MIB"),
    ("alarm", "system", "99", "ACME2-MIB"),
    ("vendorRoot", "mib-2", "9999", "ACME2-MIB"),
)
# 抽出から落ちた宣言（ラベル付きフルパスの右辺）
CHAIN_LOCAL_NAMES = {"ACME2-MIB": {"mib-2"}}
# 内蔵表。mib-2 は借りてよい親、system は借りてはいけない同名
CHAIN_KNOWN = {"mib-2": "1.3.6.1.2.1", "system": "1.3.6.1.2.1.1"}


class MibBorrowRoundWaitsForAPendingParentTest(unittest.TestCase):
    def _resolver(self, files):
        """exe の隣に mibs/ を作り、files を置いて読み込む。

        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-round-")
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

    def _resolve(self, definitions, local_names, known):
        """__init__ を通さずに _resolve_definitions だけを動かす。"""
        from core.mib_resolver import MIBResolver
        resolver = MIBResolver.__new__(MIBResolver)
        resolver.name_to_oid = dict(known)
        resolver.oid_to_name = {}
        resolver._module_local_names = {k: set(v)
                                        for k, v in local_names.items()}
        with contextlib.redirect_stdout(io.StringIO()):
            return resolver._resolve_definitions(list(definitions))

    def test_a_child_waits_while_the_root_of_the_chain_borrows(self):
        """鎖の根が借りる回に、子が内蔵の同名で確定しないこと。"""
        resolver = self._resolver({"ACME2-MIB.my": ACME2_MIB})

        self.assertEqual(resolver.resolve_name("vendorRoot"),
                         "1.3.6.1.2.1.9999")
        self.assertEqual(resolver.resolve_name("system"),
                         "1.3.6.1.2.1.9999.1")
        self.assertEqual(resolver.resolve_name("alarm"),
                         "1.3.6.1.2.1.9999.1.99",
                         "借用の回の中で内蔵の同名を借りて誤確定している")

    def test_the_vendor_oid_shows_the_vendor_name(self):
        """利用者から見える形でも、ベンダーの OID に名前が出ること。"""
        resolver = self._resolver({"ACME2-MIB.my": ACME2_MIB})

        self.assertEqual(resolver.resolve_oid("1.3.6.1.2.1.9999.1.99"),
                         "alarm", "ベンダーの Trap に名前が付かない")

    def test_the_standard_subtree_does_not_get_the_vendor_name(self):
        """標準の system 配下に、他社の名前が入らないこと。"""
        resolver = self._resolver({"ACME2-MIB.my": ACME2_MIB})

        shown = resolver.resolve_oid("1.3.6.1.2.1.1.99")
        self.assertNotEqual(shown, "alarm",
                            "標準 system 配下に他社の名前が入っている")
        self.assertEqual(shown, "system.99")

    def test_any_order_of_the_chain_gives_the_same_oids(self):
        """どの並びで渡しても、鎖が同じ OID に解決すること。"""
        for order in itertools.permutations(CHAIN):
            with self.subTest(order=[name for name, _, _, _ in order]):
                resolved = self._resolve(order, CHAIN_LOCAL_NAMES,
                                         CHAIN_KNOWN)

                self.assertEqual(resolved.get("1.3.6.1.2.1.9999"),
                                 "vendorRoot")
                self.assertEqual(resolved.get("1.3.6.1.2.1.9999.1"),
                                 "system")
                self.assertEqual(resolved.get("1.3.6.1.2.1.9999.1.99"),
                                 "alarm",
                                 "並びしだいで子が内蔵の同名を借りている")
                self.assertNotIn("1.3.6.1.2.1.1.99", resolved,
                                 "標準 system 配下に他社の名前が入っている")

    def test_a_parent_no_pending_definition_defines_is_still_borrowed(self):
        """対照: 残りの定義が親にならない名前は、これまでどおり借りること。"""
        resolved = self._resolve(
            [("directory", "internet", "1", "SMI-MIB")],
            {"SMI-MIB": {"internet"}},
            {"internet": "1.3.6.1"})

        self.assertEqual(resolved.get("1.3.6.1.1"), "directory",
                         "借用を遅らせるだけのはずが、やめてしまっている")


if __name__ == "__main__":
    unittest.main()
