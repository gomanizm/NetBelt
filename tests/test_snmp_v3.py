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


    # 実測した OID。定数名ではなく、実際に線に乗る値で固定する。
    # 名前で固定すると実装の表を写すだけになり、取り違えを検知できない。
    AUTH_OIDS = {
        "none":    (1, 3, 6, 1, 6, 3, 10, 1, 1, 1),
        "MD5":     (1, 3, 6, 1, 6, 3, 10, 1, 1, 2),
        "SHA":     (1, 3, 6, 1, 6, 3, 10, 1, 1, 3),
        "SHA-224": (1, 3, 6, 1, 6, 3, 10, 1, 1, 4),
        "SHA-256": (1, 3, 6, 1, 6, 3, 10, 1, 1, 5),
        "SHA-384": (1, 3, 6, 1, 6, 3, 10, 1, 1, 6),
        "SHA-512": (1, 3, 6, 1, 6, 3, 10, 1, 1, 7),
    }
    PRIV_OIDS = {
        "none":    (1, 3, 6, 1, 6, 3, 10, 1, 2, 1),
        "DES":     (1, 3, 6, 1, 6, 3, 10, 1, 2, 2),
        "3DES":    (1, 3, 6, 1, 6, 3, 10, 1, 2, 3),
        "AES-128": (1, 3, 6, 1, 6, 3, 10, 1, 2, 4),
        # AES-192/256 は Reeder 版。Blumenthal 版は末尾が 1 / 2 になる
        "AES-192": (1, 3, 6, 1, 4, 1, 9, 12, 6, 1, 101),
        "AES-256": (1, 3, 6, 1, 4, 1, 9, 12, 6, 1, 102),
    }

    def test_every_auth_name_maps_to_the_right_wire_protocol(self):
        """名前と定数の対応を1つずつ固定する。

        pysnmp の定数名は「HMAC<出力ビット長>SHA<ダイジェスト長>」の順で、
        SHA-256 は usmHMAC192SHA256AuthProtocol。数字が2つ並ぶため
        取り違えやすく、しかも取り違えても例外は出ない。
        """
        from core.snmp_manager import V3_AUTH_PROTOCOL_NAMES, resolve_v3_protocols
        self.assertEqual(set(V3_AUTH_PROTOCOL_NAMES), set(self.AUTH_OIDS),
                         "選択肢が増減したらこの表も更新すること")
        for name, oid in self.AUTH_OIDS.items():
            with self.subTest(auth=name):
                auth, _priv = resolve_v3_protocols(name, "none")
                self.assertEqual(tuple(auth), oid)

    def test_every_priv_name_maps_to_the_right_wire_protocol(self):
        """AES-192/256 は Reeder 版であること（名前が短い方が非標準）。"""
        from core.snmp_manager import V3_PRIV_PROTOCOL_NAMES, resolve_v3_protocols
        self.assertEqual(set(V3_PRIV_PROTOCOL_NAMES), set(self.PRIV_OIDS),
                         "選択肢が増減したらこの表も更新すること")
        for name, oid in self.PRIV_OIDS.items():
            with self.subTest(priv=name):
                auth_name = "none" if name == "none" else "SHA"
                _auth, priv = resolve_v3_protocols(auth_name, name)
                self.assertEqual(tuple(priv), oid)

    def test_aes_is_not_the_blumenthal_variant(self):
        """取り違えると相互接続できない組み合わせを明示的に弾く。"""
        from pysnmp.hlapi import (usmAesBlumenthalCfb192Protocol,
                                  usmAesBlumenthalCfb256Protocol)
        from core.snmp_manager import resolve_v3_protocols
        for name, blumenthal in (("AES-192", usmAesBlumenthalCfb192Protocol),
                                 ("AES-256", usmAesBlumenthalCfb256Protocol)):
            with self.subTest(priv=name):
                _auth, priv = resolve_v3_protocols("SHA", name)
                self.assertNotEqual(tuple(priv), tuple(blumenthal))
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



class PysnmpUnavailableTest(unittest.TestCase):
    """pysnmp を import できない環境での振る舞い。

    snmp_manager.py は pysnmp の import を try/except で囲み、失敗しても
    モジュール自体は読める作りになっている。その分岐を固定する。
    """

    def test_resolve_raises_when_pysnmp_is_missing(self):
        from unittest import mock
        import core.snmp_manager as snmp_manager
        with mock.patch.object(snmp_manager, "_PYSNMP_AVAILABLE", False):
            with self.assertRaises(RuntimeError):
                snmp_manager.resolve_v3_protocols("SHA", "AES-128")

    def test_worker_reports_the_missing_library_instead_of_raising(self):
        """利用者にはエラー文字列で伝わり、例外は外へ出ないこと。"""
        from unittest import mock
        import core.snmp_manager as snmp_manager
        worker = snmp_manager.SNMPWorker("get", {"version": "v3"})
        results = []
        worker.result_ready.connect(lambda ok, payload: results.append((ok, payload)))
        with mock.patch.object(snmp_manager, "_PYSNMP_AVAILABLE", False):
            worker.run()
        self.assertEqual(len(results), 1)
        ok, payload = results[0]
        self.assertFalse(ok)
        self.assertIn("pysnmp", payload)

class SnmpPanelV3UiTest(unittest.TestCase):
    """GET/WALK の v3 認証 UI。"""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from unittest import mock
        from ui.main_window import MainWindow
        # 起動時の更新チェックは実際に GitHub API を叩くのでモックする
        with mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            return MainWindow().snmp_panel

    def test_auth_tabs_include_a_v3_tab(self):
        panel = self._panel()
        titles = [panel.auth_tabs.tabText(i) for i in range(panel.auth_tabs.count())]
        self.assertIn("v3認証", titles)

    def test_protocol_choices_match_the_core_tables(self):
        from core.snmp_manager import V3_AUTH_PROTOCOL_NAMES, V3_PRIV_PROTOCOL_NAMES
        from ui.snmp_panel import SNMPPanel
        self.assertEqual(
            tuple(key for _label, key in SNMPPanel.AUTH_PROTOCOL_CHOICES),
            V3_AUTH_PROTOCOL_NAMES)
        self.assertEqual(
            tuple(key for _label, key in SNMPPanel.PRIV_PROTOCOL_CHOICES),
            V3_PRIV_PROTOCOL_NAMES)

    def test_password_fields_are_masked(self):
        from PyQt6.QtWidgets import QLineEdit
        panel = self._panel()
        self.assertEqual(panel.v3_auth_password_edit.echoMode(),
                         QLineEdit.EchoMode.Password)
        self.assertEqual(panel.v3_priv_password_edit.echoMode(),
                         QLineEdit.EchoMode.Password)

    def test_selecting_v3_switches_to_the_v3_tab(self):
        panel = self._panel()
        panel.version_combo.setCurrentText("v3")
        self.assertEqual(panel.auth_tabs.tabText(panel.auth_tabs.currentIndex()),
                         "v3認証")
        panel.version_combo.setCurrentText("v2c")
        self.assertEqual(panel.auth_tabs.tabText(panel.auth_tabs.currentIndex()),
                         "v1/v2c認証")

    def test_get_passes_v3_credentials(self):
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.host_edit.setText("192.0.2.10")
        panel.oid_edit.setText("1.3.6.1.2.1.1.1.0")
        panel.version_combo.setCurrentText("v3")
        panel.v3_username_edit.setText("netbelt")
        panel.v3_auth_combo.setCurrentIndex(
            [k for _l, k in panel.AUTH_PROTOCOL_CHOICES].index("SHA-256"))
        panel.v3_auth_password_edit.setText("authpass12345")
        panel.v3_priv_combo.setCurrentIndex(
            [k for _l, k in panel.PRIV_PROTOCOL_CHOICES].index("AES-128"))
        panel.v3_priv_password_edit.setText("privpass12345")

        panel._on_get_clicked()

        kwargs = panel.snmp_manager.snmp_get.call_args.kwargs
        self.assertEqual(kwargs["version"], "v3")
        self.assertEqual(kwargs["username"], "netbelt")
        self.assertEqual(kwargs["auth_protocol"], "SHA-256")
        self.assertEqual(kwargs["auth_password"], "authpass12345")
        self.assertEqual(kwargs["priv_protocol"], "AES-128")
        self.assertEqual(kwargs["priv_password"], "privpass12345")

    def test_walk_passes_v3_credentials(self):
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.host_edit.setText("192.0.2.10")
        panel.oid_edit.setText("1.3.6.1.2.1.1")
        panel.version_combo.setCurrentText("v3")
        panel.v3_username_edit.setText("netbelt")

        panel._on_walk_clicked()

        kwargs = panel.snmp_manager.snmp_walk.call_args.kwargs
        self.assertEqual(kwargs["version"], "v3")
        self.assertEqual(kwargs["username"], "netbelt")

    def test_v2c_still_passes_the_community(self):
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.host_edit.setText("192.0.2.10")
        panel.oid_edit.setText("1.3.6.1.2.1.1.1.0")
        panel.version_combo.setCurrentText("v2c")
        panel.community_edit.setText("netbelt-ro")

        panel._on_get_clicked()

        kwargs = panel.snmp_manager.snmp_get.call_args.kwargs
        self.assertEqual(kwargs["community"], "netbelt-ro")


if __name__ == "__main__":
    unittest.main()
