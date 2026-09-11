"""定義の抽出が、隣の定義の `::=` を飲み込まないことを検証する。

抽出は「型キーワードから次の `::= { 名前 数字 }` まで最短一致」で
拾っていた。右辺がその形でないと（`{ x 0 1 }` のような複数添字、
`mib-2` のようにハイフンを含む親）、`.*?` がそこで止まれずに次の
定義の `::=` まで伸びる。すると

  - 名前が隣の定義の OID に黙って結び付く（実測: a が root.2 に付いた）
  - 飲み込まれた隣の定義（b）が消える
  - `mib-2` は `\\w+` に掛からず、名前 '2' として拾われる

標準 MIB の親はほぼ全部 mib-2 なので、利用者が IF-MIB などを mibs/ に
置いた時点でこれに当たる。誤った対応はキャッシュにも残る。

右辺は `{ 名前 数字 }` ちょうどに限り、本体は自分の `::=` と、別の定義
が始まる行を越えない。名前と親にはハイフンを許す。
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

SMI_HEAD = "TEST-SMI DEFINITIONS ::= BEGIN\n"
SMI_TAIL = "\nEND\n"


class MibAssignmentBoundariesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-mib-ab-")
        self.mibs = os.path.join(self.dir, "mibs")
        os.makedirs(self.mibs)
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._cwd)

    def _write(self, name, body):
        path = os.path.join(self.mibs, name)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(SMI_HEAD + body + SMI_TAIL)
        return path

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def _resolved(self):
        return self._resolver()._load_or_update_mib_cache("mibs")

    def _extracted(self, path):
        """(名前, 親, 添字) の並び。"""
        return [tuple(d[:3])
                for d in self._resolver()._extract_mib_definitions(path)]

    def test_a_multi_subidentifier_assignment_does_not_swallow_the_next(self):
        """`::= { root 0 1 }` の定義が、次の定義の OID を奪わないこと。"""
        self._write("A.my",
                    "root OBJECT IDENTIFIER ::= { enterprises 99999 }\n"
                    "a OBJECT-TYPE\n"
                    "    SYNTAX INTEGER\n"
                    "    ::= { root 0 1 }\n"
                    "b OBJECT-TYPE\n"
                    "    SYNTAX INTEGER\n"
                    "    ::= { root 2 }\n")
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.99999.2"), "b",
                         "a が b の OID を奪っている: %s" % resolved)
        self.assertIn("b", set(resolved.values()), "b が飲み込まれて消えた")

    def test_a_hyphenated_parent_is_read_whole(self):
        """`mib-2` が名前 '2' にならず、その配下も正しい親に付くこと。"""
        path = self._write(
            "IF.my",
            "mib-2 OBJECT IDENTIFIER ::= { mgmt 1 }\n"
            "ifMIB MODULE-IDENTITY\n"
            "    LAST-UPDATED \"200006140000Z\"\n"
            "    ::= { mib-2 31 }\n"
            "ifMIBObjects OBJECT IDENTIFIER ::= { ifMIB 1 }\n"
            "ifXTable OBJECT-TYPE\n"
            "    SYNTAX SEQUENCE OF IfXEntry\n"
            "    ::= { ifMIBObjects 1 }\n"
            "ifRcvAddressTable OBJECT-TYPE\n"
            "    SYNTAX SEQUENCE OF IfRcvAddressEntry\n"
            "    ::= { ifMIBObjects 4 }\n"
            "ifTestTable OBJECT-TYPE\n"
            "    SYNTAX SEQUENCE OF IfTestEntry\n"
            "    ::= { mib-2 99 }\n")
        extracted = self._extracted(path)
        self.assertIn(("mib-2", "mgmt", "1"), extracted,
                      "mib-2 を名前として拾えていない: %s" % extracted)
        self.assertIn(("ifMIB", "mib-2", "31"), extracted)
        self.assertIn(("ifTestTable", "mib-2", "99"), extracted,
                      "ifTestTable が別の親に付いている: %s" % extracted)
        self.assertIn(("ifRcvAddressTable", "ifMIBObjects", "4"), extracted,
                      "ifRcvAddressTable が飲み込まれて消えた")
        names = [d[0] for d in extracted]
        self.assertNotIn("2", names, "mib-2 が '2' として拾われている")

    def test_a_hyphenated_parent_resolves(self):
        """ハイフン付きの親にぶら下がる定義が解決すること。"""
        self._write("H.my",
                    "my-root OBJECT IDENTIFIER ::= { enterprises 77777 }\n"
                    "myLeaf OBJECT IDENTIFIER ::= { my-root 3 }\n")
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.77777.3"), "myLeaf",
                         "ハイフン付きの親が解決できない: %s" % resolved)

    def test_imports_listing_module_identity_does_not_eat_the_module_root(self):
        """IMPORTS の直後に MODULE-IDENTITY があっても、根を拾うこと。

        `IMPORTS\\n    MODULE-IDENTITY, ...` は「名前 MODULE-IDENTITY」に
        見える。そこから次の `::=` まで伸びると、本物の根の定義を
        飲み込んで IMPORTS という偽の名前を登録する。
        """
        self._write("M.my",
                    "IMPORTS\n"
                    "    MODULE-IDENTITY, OBJECT-TYPE\n"
                    "        FROM SNMPv2-SMI;\n"
                    "fooMIB MODULE-IDENTITY\n"
                    "    LAST-UPDATED \"202601010000Z\"\n"
                    "    ::= { enterprises 1234 }\n")
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.1234"), "fooMIB",
                         "根が消えている: %s" % resolved)
        self.assertNotIn("IMPORTS", set(resolved.values()))


if __name__ == "__main__":
    unittest.main()
