"""SNMP Trap の「許可コミュニティ」が実際にフィルタとして働くことを確認する。

コミュニティ文字列は SNMPv1/v2c における合言葉。UI に「許可コミュニティ」の
入力欄があるのに照合していないと、0.0.0.0:162 に届いた任意の送信元・任意の
コミュニティの Trap がすべて受理され、入力欄が何の意味も持たなくなる。

（v2c のコミュニティは平文で流れるため強固な認証ではないが、誤送信や
別システムの Trap を弾く実用的な効果はある。）
"""
import sys
import unittest

sys.path.insert(0, "src")

ADDR = ("192.0.2.10", 4242)


def _trap_bytes(community):
    """指定したコミュニティを持つ SNMPv2c Trap のバイト列を組み立てる。"""
    from pyasn1.codec.ber import encoder
    from pysnmp.proto import api

    pMod = api.protoModules[api.protoVersion2c]
    pdu = pMod.TrapPDU()
    pMod.apiTrapPDU.setDefaults(pdu)
    msg = pMod.Message()
    pMod.apiMessage.setDefaults(msg)
    pMod.apiMessage.setCommunity(msg, community)
    pMod.apiMessage.setPDU(msg, pdu)
    return encoder.encode(msg)


class SnmpCommunityFilterTest(unittest.TestCase):
    def _receiver(self, communities):
        from core.snmp_manager import SNMPTrapReceiver
        return SNMPTrapReceiver(port=0, communities=communities)

    def test_matching_community_is_accepted(self):
        r = self._receiver(["public"])
        self.assertIsNotNone(r._parse_snmp_trap(_trap_bytes("public"), ADDR),
                             "許可したコミュニティの Trap が受理されない")

    def test_mismatched_community_is_dropped(self):
        r = self._receiver(["secret-community"])
        self.assertIsNone(r._parse_snmp_trap(_trap_bytes("public"), ADDR),
                          "許可していないコミュニティの Trap が受理された")

    def test_multiple_allowed_communities(self):
        r = self._receiver(["public", "netbelt", "ops"])
        for c in ("public", "netbelt", "ops"):
            with self.subTest(community=c):
                self.assertIsNotNone(r._parse_snmp_trap(_trap_bytes(c), ADDR))
        self.assertIsNone(r._parse_snmp_trap(_trap_bytes("other"), ADDR))

    def test_community_match_is_case_sensitive(self):
        """コミュニティ名は大文字小文字を区別する（SNMP の仕様どおり）。"""
        r = self._receiver(["Public"])
        self.assertIsNone(r._parse_snmp_trap(_trap_bytes("public"), ADDR))

    def test_default_is_public(self):
        """未指定なら public のみを許可する（従来の既定を維持）。"""
        r = self._receiver(None)
        self.assertIsNotNone(r._parse_snmp_trap(_trap_bytes("public"), ADDR))
        self.assertIsNone(r._parse_snmp_trap(_trap_bytes("private"), ADDR))

    def test_parsed_trap_keeps_source_and_varbinds(self):
        """受理した Trap の中身がこれまでどおり取り出せること。"""
        r = self._receiver(["public"])
        got = r._parse_snmp_trap(_trap_bytes("public"), ADDR)
        self.assertEqual(got["source_ip"], ADDR[0])
        self.assertEqual(got["source_port"], ADDR[1])
        self.assertIn("varbinds", got)


if __name__ == "__main__":
    unittest.main()
