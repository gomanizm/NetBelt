"""標準 OID ツリーの起点が初期辞書にあることを検証する。

初期辞書は葉の OID しか持たず、iso / org / dod / internet / mgmt /
mib-2 / private / enterprises / snmpV2 が入っていなかった。iso は
ASN.1 の暗黙の根なのでどの MIB ファイルにも定義が無く、標準 MIB を
mibs/ へ置いても `::= { mib-2 n }` の親がどこからも引けない。解決は
そこで連鎖ごと止まり、その MIB は丸ごと無効になる（知らせるのは
標準出力の「0件」だけ）。

起点を初期辞書へ入れておけば、標準 MIB も mibs/ から読める。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

SMI_HEAD = "TEST-SMI DEFINITIONS ::= BEGIN\n"
SMI_TAIL = "\nEND\n"


class MibStandardRootsTest(unittest.TestCase):
    def setUp(self):
        # exe の隣の mibs/ を読む凍結ビルドとして動かす
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-roots-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        self.mibs = os.path.join(self.exe_dir, "mibs")
        os.makedirs(self.mibs)

    def _write_mib(self, name, body):
        with io.open(os.path.join(self.mibs, name), "w",
                     encoding="utf-8") as f:
            f.write(SMI_HEAD + body + SMI_TAIL)

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_the_roots_of_the_standard_tree_are_known(self):
        """起点の OID が名前で引けること。"""
        resolver = self._resolver()
        for name, oid in (("iso", "1"),
                          ("org", "1.3"),
                          ("dod", "1.3.6"),
                          ("internet", "1.3.6.1"),
                          ("mgmt", "1.3.6.1.2"),
                          ("mib-2", "1.3.6.1.2.1"),
                          ("private", "1.3.6.1.4"),
                          ("enterprises", "1.3.6.1.4.1"),
                          ("snmpV2", "1.3.6.1.6")):
            self.assertEqual(resolver.resolve_name(name), oid,
                             "%s が初期辞書に無い" % name)
            self.assertEqual(resolver.resolve_oid(oid), name)

    def test_a_definition_hanging_off_mib_2_resolves(self):
        """mib-2 を親にする定義が解決すること。"""
        self._write_mib("STD-USE.my",
                        "stdThing OBJECT IDENTIFIER ::= { mib-2 9999 }\n"
                        "stdLeaf OBJECT-TYPE\n"
                        "    SYNTAX Integer32\n"
                        "    ::= { stdThing 1 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("stdThing"),
                         "1.3.6.1.2.1.9999",
                         "mib-2 配下が解決していない")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.2.1.9999.1"), "stdLeaf")

    def test_a_chain_that_starts_at_iso_resolves(self):
        """iso から書き下した連鎖が解決すること。

        標準 MIB は `internet ::= { iso org(3) dod(6) 1 }` のような
        書き方こそしないが、SMI は根を iso から辿る。iso が無いと
        そこで連鎖ごと止まる。
        """
        self._write_mib("ISO-CHAIN.my",
                        "isoChild OBJECT IDENTIFIER ::= { iso 9 }\n"
                        "isoGrand OBJECT IDENTIFIER ::= { isoChild 4 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("isoGrand"), "1.9.4",
                         "iso から始まる連鎖が解決していない")

    def test_a_definition_under_enterprises_still_resolves(self):
        """enterprises 配下はこれまでどおり解決すること。"""
        self._write_mib("ENT.my",
                        "entRoot OBJECT IDENTIFIER ::= { enterprises 99999 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("entRoot"),
                         "1.3.6.1.4.1.99999")


if __name__ == "__main__":
    unittest.main()
