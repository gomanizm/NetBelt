"""OBJECT-IDENTITY の定義を抽出することを検証する。

OBJECT-IDENTITY は「別の定義が始まる行」を見分けるキーワードには
入っていたが、抽出する形（_MIB_DEFINITION_PATTERNS）には入っていな
かった。ベンダー MIB は中間ノードを OBJECT-IDENTITY で置くことが
多く、その節が落ちると配下が丸ごと解決できない（数値 OID のまま、
あるいは親の名前＋添字の形で出る）。誤情報にはならないが、部分的に
欠けるので気づきにくい。
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


class MibObjectIdentityTest(unittest.TestCase):
    def setUp(self):
        # exe の隣の mibs/ を読む凍結ビルドとして動かす
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-identity-")
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

    def test_an_object_identity_node_is_extracted(self):
        """OBJECT-IDENTITY の節そのものが解決すること。"""
        self._write_mib("OI.my",
                        "oVendor OBJECT IDENTIFIER ::= { enterprises 33333 }\n"
                        "oNode OBJECT-IDENTITY\n"
                        "    STATUS current\n"
                        '    DESCRIPTION "a middle node"\n'
                        "    ::= { oVendor 1 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("oNode"),
                         "1.3.6.1.4.1.33333.1",
                         "OBJECT-IDENTITY の節を拾えていない")

    def test_what_hangs_under_an_object_identity_node_resolves(self):
        """その配下も解決すること。"""
        self._write_mib("OI.my",
                        "oVendor OBJECT IDENTIFIER ::= { enterprises 33333 }\n"
                        "oNode OBJECT-IDENTITY\n"
                        "    STATUS current\n"
                        '    DESCRIPTION "a middle node"\n'
                        "    ::= { oVendor 1 }\n"
                        "oTrap NOTIFICATION-TYPE\n"
                        "    STATUS current\n"
                        "    ::= { oNode 5 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.33333.1.5"),
                         "oTrap",
                         "OBJECT-IDENTITY の配下が丸ごと落ちている")

    def test_a_plain_assignment_under_an_object_identity_node_resolves(self):
        """単純な `::= { 親 数字 }` の子も解決すること。"""
        self._write_mib("OI.my",
                        "oVendor OBJECT IDENTIFIER ::= { enterprises 33333 }\n"
                        "oNode OBJECT-IDENTITY\n"
                        "    STATUS current\n"
                        "    ::= { oVendor 1 }\n"
                        "oChild OBJECT IDENTIFIER ::= { oNode 2 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("oChild"),
                         "1.3.6.1.4.1.33333.1.2")

    def test_a_definition_after_an_object_identity_is_not_swallowed(self):
        """OBJECT-IDENTITY の次に来る定義を飲み込まないこと。"""
        self._write_mib("OI.my",
                        "oVendor OBJECT IDENTIFIER ::= { enterprises 33333 }\n"
                        "oNode OBJECT-IDENTITY\n"
                        "    STATUS current\n"
                        "    ::= { oVendor 1 }\n"
                        "oOther OBJECT-IDENTITY\n"
                        "    STATUS current\n"
                        "    ::= { oVendor 2 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("oNode"),
                         "1.3.6.1.4.1.33333.1")
        self.assertEqual(resolver.resolve_name("oOther"),
                         "1.3.6.1.4.1.33333.2")


if __name__ == "__main__":
    unittest.main()
