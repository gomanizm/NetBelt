"""SNMPv3（USM）のプロトコル選択と認証データ組み立て。"""
import sys
import unittest

sys.path.insert(0, "src")


class ResolveV3ProtocolsTest(unittest.TestCase):
    """文字列から pysnmp の USM プロトコル定数を引く。"""

    def test_auth_names_cover_md5_sha1_and_sha2(self):
        from core.snmp_manager import V3_AUTH_PROTOCOL_NAMES
        self.assertEqual(
            V3_AUTH_PROTOCOL_NAMES,
            ("none", "MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512"))

    def test_priv_names_cover_des_3des_and_aes(self):
        from core.snmp_manager import V3_PRIV_PROTOCOL_NAMES
        self.assertEqual(
            V3_PRIV_PROTOCOL_NAMES,
            ("none", "DES", "3DES", "AES-128", "AES-192", "AES-256"))

    def test_sha256_maps_to_the_hmac192_constant(self):
        """SHA-256 は usmHMAC192SHA256AuthProtocol。名前の数字が紛らわしい。"""
        from pysnmp.hlapi import usmHMAC192SHA256AuthProtocol
        from core.snmp_manager import resolve_v3_protocols
        auth, _priv = resolve_v3_protocols("SHA-256", "none")
        self.assertEqual(auth, usmHMAC192SHA256AuthProtocol)

    def test_aes256_maps_to_the_reeder_variant(self):
        """ベンダー実装と相互接続するのは Reeder 版（名前が短い方）。"""
        from pysnmp.hlapi import usmAesCfb256Protocol, usmAesBlumenthalCfb256Protocol
        from core.snmp_manager import resolve_v3_protocols
        _auth, priv = resolve_v3_protocols("SHA", "AES-256")
        self.assertEqual(priv, usmAesCfb256Protocol)
        self.assertNotEqual(priv, usmAesBlumenthalCfb256Protocol)

    def test_every_name_resolves(self):
        from core.snmp_manager import (
            V3_AUTH_PROTOCOL_NAMES, V3_PRIV_PROTOCOL_NAMES, resolve_v3_protocols)
        for auth_name in V3_AUTH_PROTOCOL_NAMES:
            for priv_name in V3_PRIV_PROTOCOL_NAMES:
                if auth_name == "none" and priv_name != "none":
                    continue  # 別テストで例外を確認する
                with self.subTest(auth=auth_name, priv=priv_name):
                    resolve_v3_protocols(auth_name, priv_name)

    def test_unknown_auth_name_raises(self):
        """黙って noAuth に落とさない（認証失敗の原因が追えなくなるため）。"""
        from core.snmp_manager import resolve_v3_protocols
        with self.assertRaises(ValueError):
            resolve_v3_protocols("SHA512", "none")   # 正しくは SHA-512

    def test_unknown_priv_name_raises(self):
        from core.snmp_manager import resolve_v3_protocols
        with self.assertRaises(ValueError):
            resolve_v3_protocols("SHA", "AES")       # 正しくは AES-128

    def test_priv_without_auth_raises(self):
        """SNMPv3 では authNoPriv 以上でないと暗号化は使えない。"""
        from core.snmp_manager import resolve_v3_protocols
        with self.assertRaises(ValueError) as ctx:
            resolve_v3_protocols("none", "AES-128")
        self.assertIn("認証", str(ctx.exception))


class PrepareAuthDataTest(unittest.TestCase):
    """SNMPWorker._prepare_auth_data が正しい UsmUserData を作ること。"""

    def _worker(self, params):
        from core.snmp_manager import SNMPWorker
        return SNMPWorker("get", params)

    def test_v3_no_auth_no_priv(self):
        w = self._worker({"username": "netbelt"})
        data = w._prepare_auth_data("v3")
        self.assertEqual(str(data.userName), "netbelt")
        self.assertEqual(data.securityLevel, "noAuthNoPriv")

    def test_v3_auth_no_priv(self):
        w = self._worker({"username": "netbelt", "auth_protocol": "SHA-256",
                          "auth_password": "authpass12345"})
        data = w._prepare_auth_data("v3")
        self.assertEqual(data.securityLevel, "authNoPriv")

    def test_v3_auth_priv(self):
        from pysnmp.hlapi import usmHMAC192SHA256AuthProtocol, usmAesCfb128Protocol
        w = self._worker({"username": "netbelt", "auth_protocol": "SHA-256",
                          "auth_password": "authpass12345",
                          "priv_protocol": "AES-128", "priv_password": "privpass12345"})
        data = w._prepare_auth_data("v3")
        self.assertEqual(data.securityLevel, "authPriv")
        self.assertEqual(data.authProtocol, usmHMAC192SHA256AuthProtocol)
        self.assertEqual(data.privProtocol, usmAesCfb128Protocol)

    def test_v3_priv_without_auth_raises(self):
        w = self._worker({"username": "netbelt", "auth_protocol": "none",
                          "priv_protocol": "AES-128", "priv_password": "privpass12345"})
        with self.assertRaises(ValueError):
            w._prepare_auth_data("v3")

    def test_v1_and_v2c_are_unchanged(self):
        w = self._worker({"community": "netbelt"})
        self.assertEqual(w._prepare_auth_data("v1").mpModel, 0)
        self.assertEqual(w._prepare_auth_data("v2c").mpModel, 1)


if __name__ == "__main__":
    unittest.main()
