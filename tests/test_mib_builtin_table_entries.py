# -*- coding: utf-8 -*-
"""内蔵 MIB 表の個々の行が正しい OID を指していることを検証する。

* bgpPeerRemoteAs が 1.3.6.1.2.1.15.3.1.7（本当は bgpPeerRemoteAddr）に
  登録されていた。最長一致なので bgpPeerRemoteAddr の varbind が
  「bgpPeerRemoteAs.<peer>」と出て、本物の .9 は「bgpPeerEntry.9.<peer>」に
  なっていた。
* mib-2 の下の transmission（1.3.6.1.2.1.10）が無く、SNMPv2-SMI を置かずに
  EtherLike-MIB など（dot3 ::= { transmission 7 }）を mibs/ へ置くと、その木が
  解決できなかった。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class MibBuiltinTableEntriesTest(unittest.TestCase):
    def setUp(self):
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-builtin-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        self.mibs = os.path.join(self.exe_dir, "mibs")
        os.makedirs(self.mibs)

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_bgp_peer_remote_as_and_addr(self):
        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name('bgpPeerRemoteAs'),
                         '1.3.6.1.2.1.15.3.1.9')
        self.assertEqual(resolver.resolve_name('bgpPeerRemoteAddr'),
                         '1.3.6.1.2.1.15.3.1.7')
        self.assertEqual(resolver.resolve_oid('1.3.6.1.2.1.15.3.1.9.192.0.2.1'),
                         'bgpPeerRemoteAs.192.0.2.1')
        self.assertEqual(resolver.resolve_oid('1.3.6.1.2.1.15.3.1.7.192.0.2.1'),
                         'bgpPeerRemoteAddr.192.0.2.1')

    def test_transmission_is_known(self):
        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name('transmission'), '1.3.6.1.2.1.10')

    def test_a_mib_under_transmission_resolves(self):
        """transmission の下に置く MIB が mibs/ から読めること。"""
        with io.open(os.path.join(self.mibs, "TEST-ETHER-MIB.mib"), "w",
                     encoding="utf-8") as f:
            f.write("TEST-ETHER-MIB DEFINITIONS ::= BEGIN\n"
                    "dot3 OBJECT IDENTIFIER ::= { transmission 7 }\n"
                    "END\n")
        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name('dot3'), '1.3.6.1.2.1.10.7')


if __name__ == "__main__":
    unittest.main()
