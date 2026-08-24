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


class V3PasswordErrorTest(unittest.TestCase):
    """v3 のパスワード長の検査。

    RFC 3414 は USM のパスワードに8文字以上を要求する。それより短いと
    pysnmp は空文字で ZeroDivisionError、1〜7文字で WrongValueError に
    なり、どちらも利用者には原因が読み取れない（実測）。
    """

    def test_none_protocols_need_no_password(self):
        from core.snmp_manager import v3_password_error
        self.assertIsNone(v3_password_error("none", "", "none", ""))

    def test_a_short_auth_password_is_reported(self):
        from core.snmp_manager import v3_password_error
        for password in ("", "1234567"):
            with self.subTest(password=password):
                message = v3_password_error("SHA-256", password, "none", "")
                self.assertIsNotNone(message)
                self.assertIn("認証", message)

    def test_a_short_priv_password_is_reported(self):
        from core.snmp_manager import v3_password_error
        message = v3_password_error("SHA-256", "authpass1", "AES-128", "short")
        self.assertIsNotNone(message)
        self.assertIn("暗号", message)

    def test_eight_characters_is_the_boundary(self):
        """7文字で止め、8文字で通す。境界は認証プロトコルに依らない（実測）。"""
        from core.snmp_manager import v3_password_error
        for auth in ("MD5", "SHA", "SHA-256", "SHA-512"):
            with self.subTest(auth=auth):
                self.assertIsNotNone(
                    v3_password_error(auth, "1234567", "none", ""))
                self.assertIsNone(
                    v3_password_error(auth, "12345678", "none", ""))


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

    # 作った MainWindow はクラス終了まで保持する。
    # snmp_panel だけ受け取って窓を捨てると、GC のタイミングで C++ 側の
    # ウィジェットが破棄され、あとから触ると
    # 「wrapped C/C++ object of type QLineEdit has been deleted」で落ちる。
    # 単体では通るのに全体実行で落ちる、という形で表面化する（実測）。
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def setUp(self):
        """モーダルを塞ぐ。

        offscreen でも QMessageBox は exec() で本当にブロックするため、
        検証が退行して正常入力でも警告を返すようになると、テストが
        「失敗」ではなく「ハング」になる（実測）。TrapTabV3UiTest では
        同じ理由で既に塞いであるので、こちらも揃える。
        """
        from unittest import mock
        patcher = mock.patch("ui.snmp_panel.QMessageBox.warning")
        self.warning = patcher.start()
        self.addCleanup(patcher.stop)

    def _panel(self):
        from unittest import mock
        from ui.main_window import MainWindow
        # 起動時の更新チェックは実際に GitHub API を叩くのでモックする
        with mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            window = MainWindow()
        type(self)._windows.append(window)
        return window.snmp_panel

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

    def test_get_refuses_a_short_v3_password(self):
        """短いパスワードは pysnmp の読めない例外になるので手前で止める。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.host_edit.setText("192.0.2.10")
        panel.oid_edit.setText("1.3.6.1.2.1.1.1.0")
        panel.version_combo.setCurrentText("v3")
        panel.v3_username_edit.setText("netbelt")
        panel.v3_auth_combo.setCurrentIndex(
            [k for _l, k in panel.AUTH_PROTOCOL_CHOICES].index("SHA-256"))
        panel.v3_auth_password_edit.setText("1234567")

        panel._on_get_clicked()

        self.warning.assert_called_once()
        panel.snmp_manager.snmp_get.assert_not_called()

    def test_walk_passes_v3_credentials(self):
        """GET と同じ5キーを確認する。

        username だけ見ていると、他の4キーを落としても GET のテストが
        拾ってくれるうちは気づけない。WALK が別経路になった時点で盲目になる。
        """
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.host_edit.setText("192.0.2.10")
        panel.oid_edit.setText("1.3.6.1.2.1.1")
        panel.version_combo.setCurrentText("v3")
        panel.v3_username_edit.setText("netbelt")
        panel.v3_auth_combo.setCurrentIndex(
            [k for _l, k in panel.AUTH_PROTOCOL_CHOICES].index("SHA-256"))
        panel.v3_auth_password_edit.setText("authpass12345")
        panel.v3_priv_combo.setCurrentIndex(
            [k for _l, k in panel.PRIV_PROTOCOL_CHOICES].index("AES-128"))
        panel.v3_priv_password_edit.setText("privpass12345")

        panel._on_walk_clicked()

        kwargs = panel.snmp_manager.snmp_walk.call_args.kwargs
        self.assertEqual(kwargs["version"], "v3")
        self.assertEqual(kwargs["username"], "netbelt")
        self.assertEqual(kwargs["auth_protocol"], "SHA-256")
        self.assertEqual(kwargs["auth_password"], "authpass12345")
        self.assertEqual(kwargs["priv_protocol"], "AES-128")
        self.assertEqual(kwargs["priv_password"], "privpass12345")

    def test_unused_auth_tab_is_disabled(self):
        """入力すべきタブを取り違えないよう、使わない方は無効にする。

        選択状態だけ見ても、無効化を丸ごと消したことに気づけない。
        """
        panel = self._panel()
        titles = [panel.auth_tabs.tabText(i) for i in range(panel.auth_tabs.count())]
        v2c = titles.index("v1/v2c認証")
        v3 = titles.index("v3認証")
        preset = titles.index("プリセットOID")

        panel.version_combo.setCurrentText("v3")
        self.assertFalse(panel.auth_tabs.isTabEnabled(v2c))
        self.assertTrue(panel.auth_tabs.isTabEnabled(v3))
        self.assertTrue(panel.auth_tabs.isTabEnabled(preset),
                        "プリセットOID は常に使えること")

        panel.version_combo.setCurrentText("v2c")
        self.assertTrue(panel.auth_tabs.isTabEnabled(v2c))
        self.assertFalse(panel.auth_tabs.isTabEnabled(v3))
        self.assertTrue(panel.auth_tabs.isTabEnabled(preset))


    def test_the_initial_version_decides_the_enabled_tab(self):
        """起動直後から、入力すべきタブだけが有効であること。"""
        panel = self._panel()
        titles = [panel.auth_tabs.tabText(i) for i in range(panel.auth_tabs.count())]
        self.assertEqual(panel.version_combo.currentText(), "v2c")
        self.assertTrue(panel.auth_tabs.isTabEnabled(titles.index("v1/v2c認証")))
        self.assertFalse(panel.auth_tabs.isTabEnabled(titles.index("v3認証")))

    def test_v3_without_a_username_is_refused(self):
        """UsmUserData('') の noAuthNoPriv になる経路を実行前に止めること。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.host_edit.setText("192.0.2.10")
        panel.oid_edit.setText("1.3.6.1.2.1.1.1.0")
        panel.version_combo.setCurrentText("v3")
        panel.v3_username_edit.setText("   ")

        for run, sender in ((panel._on_get_clicked, panel.snmp_manager.snmp_get),
                            (panel._on_walk_clicked, panel.snmp_manager.snmp_walk)):
            with self.subTest(run=run.__name__):
                with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
                    run()
                warn.assert_called_once()
                sender.assert_not_called()

    def test_v3_priv_without_auth_is_refused(self):
        """SNMPv3 では authNoPriv 以上でないと暗号化できない。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.host_edit.setText("192.0.2.10")
        panel.oid_edit.setText("1.3.6.1.2.1.1.1.0")
        panel.version_combo.setCurrentText("v3")
        panel.v3_username_edit.setText("netbelt-v3")
        panel.v3_auth_combo.setCurrentIndex(
            [k for _l, k in panel.AUTH_PROTOCOL_CHOICES].index("none"))
        panel.v3_priv_combo.setCurrentIndex(
            [k for _l, k in panel.PRIV_PROTOCOL_CHOICES].index("AES-128"))

        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
            panel._on_get_clicked()
        warn.assert_called_once()
        panel.snmp_manager.snmp_get.assert_not_called()

    def test_v3_does_not_send_a_community(self):
        """v3 に community は要らない。意味の無い値を運ばないこと。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.host_edit.setText("192.0.2.10")
        panel.oid_edit.setText("1.3.6.1.2.1.1.1.0")
        panel.version_combo.setCurrentText("v3")
        panel.v3_username_edit.setText("netbelt-v3")
        panel._on_get_clicked()
        self.assertNotIn("community", panel.snmp_manager.snmp_get.call_args.kwargs)
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


class V3TrapReceiveTest(unittest.TestCase):
    """v3 Trap を実際に送って受け取れること。"""

    USER = "netbelt-v3"
    AUTHKEY = "authpass12345"
    PRIVKEY = "privpass12345"
    SENDER_ENGINE_ID = "8000000001020304"

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _managers = []

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        for m in cls._managers:
            m.stop_trap_receiver()
        QApplication.processEvents()
        cls._managers.clear()

    def setUp(self):
        from unittest import mock
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _free_port(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _v3_user(self, engine_ids):
        return {"username": self.USER, "auth_protocol": "SHA-256",
                "auth_password": self.AUTHKEY, "priv_protocol": "AES-128",
                "priv_password": self.PRIVKEY, "engine_ids": engine_ids}

    def _start(self, engine_ids):
        import time
        from core.snmp_manager import SNMPManager
        m = SNMPManager()
        self._managers.append(m)
        self.addCleanup(m.stop_trap_receiver)
        port = self._free_port()
        self.assertTrue(m.start_trap_receiver(port, ["public"],
                                              [self._v3_user(engine_ids)]))
        deadline = time.time() + 5
        while time.time() < deadline:
            if m.trap_receiver and m.trap_receiver.isRunning():
                break
            time.sleep(0.05)
        got = []
        m.trap_received.connect(got.append)
        return m, port, got

    def _send_v3_trap(self, port, auth_password):
        from pysnmp.hlapi import (SnmpEngine, UsmUserData, UdpTransportTarget,
                                  ContextData, NotificationType, ObjectIdentity,
                                  sendNotification, usmHMAC192SHA256AuthProtocol,
                                  usmAesCfb128Protocol)
        from pysnmp.proto.rfc1902 import OctetString
        sender = SnmpEngine(OctetString(hexValue=self.SENDER_ENGINE_ID))
        next(sendNotification(
            sender,
            UsmUserData(self.USER, auth_password, self.PRIVKEY,
                        authProtocol=usmHMAC192SHA256AuthProtocol,
                        privProtocol=usmAesCfb128Protocol),
            UdpTransportTarget(("127.0.0.1", port)), ContextData(), "trap",
            NotificationType(ObjectIdentity("1.3.6.1.6.3.1.1.5.1"))))

    def _wait(self, got, expect_more, before, timeout=4):
        import time
        from PyQt6.QtWidgets import QApplication
        deadline = time.time() + timeout
        while time.time() < deadline:
            QApplication.processEvents()
            if expect_more and len(got) > before:
                return True
            time.sleep(0.02)
        QApplication.processEvents()
        return len(got) > before

    def test_v3_trap_is_received_when_the_engine_id_is_registered(self):
        _m, port, got = self._start([self.SENDER_ENGINE_ID])
        self._send_v3_trap(port, self.AUTHKEY)
        self.assertTrue(self._wait(got, True, 0), "v3 Trap を受信できない")
        self.assertTrue(got[-1]["varbinds"])

    def test_v3_trap_reports_the_security_level(self):
        _m, port, got = self._start([self.SENDER_ENGINE_ID])
        self._send_v3_trap(port, self.AUTHKEY)
        self.assertTrue(self._wait(got, True, 0))
        trap = got[-1]
        self.assertEqual(trap["security_name"], self.USER)
        self.assertEqual(trap["security_level"], "3")   # authPriv
        self.assertEqual(trap["security_model"], "3")   # USM

    def test_v3_trap_with_a_wrong_password_is_dropped(self):
        _m, port, got = self._start([self.SENDER_ENGINE_ID])
        self._send_v3_trap(port, "wrongpassword1")
        self.assertFalse(self._wait(got, False, 0, timeout=2),
                         "認証に失敗した v3 Trap が受理された")

    def test_v3_trap_from_an_unregistered_engine_id_is_dropped(self):
        """engineID を登録していない送信元からは受けられない（実測で確定した仕様）。"""
        _m, port, got = self._start(["80000000AABBCCDD"])
        self._send_v3_trap(port, self.AUTHKEY)
        self.assertFalse(self._wait(got, False, 0, timeout=2))

    def test_v2c_still_works_alongside_v3(self):
        """v1/v2c と v3 を同じポートで同時に受けられること。"""
        import socket
        from tests.test_snmp_community import trap_bytes
        _m, port, got = self._start([self.SENDER_ENGINE_ID])
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(trap_bytes("public"), ("127.0.0.1", port))
        s.close()
        self.assertTrue(self._wait(got, True, 0))


class ShortV3PasswordTrapTest(unittest.TestCase):
    """短い v3 パスワードで受信を始めようとしたときの伝わり方。

    以前は bind() の try の中で pysnmp が落ちるため
    「ポート 162 で待ち受けできません: integer division or modulo by zero」
    となり、原因と無関係なポート競合の調査へ誘導していた（実測）。
    """

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _managers = []

    @classmethod
    def tearDownClass(cls):
        for m in cls._managers:
            m.stop_trap_receiver()
        cls._managers.clear()

    def setUp(self):
        from unittest import mock
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _try_start(self, auth_password):
        import socket
        from core.snmp_manager import SNMPManager
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        m = SNMPManager()
        type(self)._managers.append(m)
        self.addCleanup(m.stop_trap_receiver)
        errors = []
        m.error_occurred.connect(errors.append)
        user = {"username": "netbelt-v3", "auth_protocol": "SHA-256",
                "auth_password": auth_password, "priv_protocol": "none",
                "priv_password": "", "engine_ids": ["8000000001020304"]}
        started = m.start_trap_receiver(port, ["public"], [user])
        return started, errors, port

    def test_a_short_password_is_refused_before_binding(self):
        for password in ("", "1234567"):
            with self.subTest(password=password):
                started, errors, port = self._try_start(password)
                self.assertFalse(started, "短いパスワードで受信を始めてしまった")
                self.assertTrue(errors)
                self.assertIn("パスワード", errors[-1])

    def test_the_message_does_not_blame_the_port(self):
        """ポート番号を主語にすると、ポート競合を探しに行かせてしまう。"""
        _started, errors, port = self._try_start("1234567")
        self.assertTrue(errors)
        self.assertNotIn(f"ポート {port}", errors[-1])

    def test_a_long_enough_password_still_starts(self):
        started, errors, _port = self._try_start("authpass12345")
        self.assertTrue(started, f"正常な設定で起動できない: {errors}")


class TrapTabV3UiTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作った MainWindow はクラス終了まで保持する。snmp_panel だけ受け取って
    # 窓を捨てると、GC のタイミングで C++ 側のウィジェットが破棄され、
    # あとから触ると「wrapped C/C++ object ... has been deleted」で落ちる。
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def setUp(self):
        from unittest import mock
        # 受信開始の経路は検証に失敗するとモーダルを出す。offscreen では
        # 誰も閉じられないので、テストが「失敗」ではなく「ハング」になる。
        # 個々のテストがさらに patch すればそちらが優先される。
        warn = mock.patch("ui.snmp_panel.QMessageBox.warning")
        warn.start()
        self.addCleanup(warn.stop)

    def _panel(self):
        from unittest import mock
        from ui.main_window import MainWindow
        # 起動時の更新チェックは実際に GitHub API を叩くのでモックする
        with mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            window = MainWindow()
        type(self)._windows.append(window)
        return window.snmp_panel

    def test_trap_version_combo_defaults_to_both(self):
        panel = self._panel()
        self.assertEqual(
            [panel.trap_version_combo.itemText(i)
             for i in range(panel.trap_version_combo.count())],
            ["両方", "v1/v2c", "v3"])
        self.assertEqual(panel.trap_version_combo.currentText(), "両方")

    def test_engine_ids_are_split_by_line(self):
        panel = self._panel()
        panel.trap_v3_username_edit.setText("netbelt-v3")
        panel.trap_v3_engine_ids_edit.setPlainText(
            "  8000000001020304 \n\n80000000AABBCCDD\n  \n")
        users = panel._collect_trap_v3_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["engine_ids"],
                         ["8000000001020304", "80000000AABBCCDD"])

    def test_no_v3_user_without_a_username(self):
        panel = self._panel()
        panel.trap_v3_username_edit.setText("")
        self.assertEqual(panel._collect_trap_v3_users(), [])

    def test_v1v2c_only_sends_no_v3_users(self):
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.snmp_manager.start_trap_receiver.return_value = True
        panel.mib_loading = False
        panel.mib_loaded = True
        panel.trap_version_combo.setCurrentText("v1/v2c")
        panel.trap_v3_username_edit.setText("netbelt-v3")
        panel._on_trap_start_clicked()
        args = panel.snmp_manager.start_trap_receiver.call_args
        self.assertEqual(args[0][2], [])

    def test_v3_only_sends_no_communities(self):
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.snmp_manager.start_trap_receiver.return_value = True
        panel.mib_loading = False
        panel.mib_loaded = True
        panel.trap_version_combo.setCurrentText("v3")
        panel.trap_v3_username_edit.setText("netbelt-v3")
        panel.trap_v3_engine_ids_edit.setPlainText("8000000001020304")
        panel._on_trap_start_clicked()
        args = panel.snmp_manager.start_trap_receiver.call_args
        self.assertEqual(args[0][1], [])
        self.assertEqual(len(args[0][2]), 1)

    def test_both_sends_communities_and_v3_users(self):
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.snmp_manager.start_trap_receiver.return_value = True
        panel.mib_loading = False
        panel.mib_loaded = True
        panel.trap_version_combo.setCurrentText("両方")
        panel.trap_community_edit.setText("public")
        panel.trap_v3_username_edit.setText("netbelt-v3")
        panel.trap_v3_engine_ids_edit.setPlainText("8000000001020304")
        panel._on_trap_start_clicked()
        args = panel.snmp_manager.start_trap_receiver.call_args
        self.assertEqual(args[0][1], ["public"])
        self.assertEqual(len(args[0][2]), 1)

    def test_trap_v3_username_without_engine_ids_is_refused(self):
        """v3 Trap は送信元の EngineID を登録しないと1件も受信できない
        （実測で確認済み）。原因の分かりにくい「起動したのに何も来ない」
        を避けるため、実行前に止めること。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.snmp_manager.start_trap_receiver.return_value = True
        panel.mib_loading = False
        panel.mib_loaded = True
        panel.trap_version_combo.setCurrentText("v3")
        panel.trap_v3_username_edit.setText("netbelt-v3")

        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
            panel._on_trap_start_clicked()
        warn.assert_called_once()
        panel.snmp_manager.start_trap_receiver.assert_not_called()

    def test_trap_v3_priv_without_auth_is_refused(self):
        """SNMPv3 では authNoPriv 以上でないと暗号化できない。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.snmp_manager.start_trap_receiver.return_value = True
        panel.mib_loading = False
        panel.mib_loaded = True
        panel.trap_version_combo.setCurrentText("両方")
        panel.trap_v3_username_edit.setText("netbelt-v3")
        panel.trap_v3_engine_ids_edit.setPlainText("8000000001020304")
        panel.trap_v3_auth_combo.setCurrentIndex(
            [k for _l, k in panel.AUTH_PROTOCOL_CHOICES].index("none"))
        panel.trap_v3_priv_combo.setCurrentIndex(
            [k for _l, k in panel.PRIV_PROTOCOL_CHOICES].index("AES-128"))

        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
            panel._on_trap_start_clicked()
        warn.assert_called_once()
        panel.snmp_manager.start_trap_receiver.assert_not_called()

    def test_trap_v1v2c_skips_v3_validation_even_without_engine_ids(self):
        """バージョン選択が v1/v2c のときは v3 の入力を検証しない。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.snmp_manager.start_trap_receiver.return_value = True
        panel.mib_loading = False
        panel.mib_loaded = True
        panel.trap_version_combo.setCurrentText("v1/v2c")
        panel.trap_v3_username_edit.setText("netbelt-v3")
        panel.trap_v3_priv_combo.setCurrentIndex(
            [k for _l, k in panel.PRIV_PROTOCOL_CHOICES].index("AES-128"))

        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
            panel._on_trap_start_clicked()
        warn.assert_not_called()
        panel.snmp_manager.start_trap_receiver.assert_called_once()

    def test_trap_v3_alone_without_a_username_is_refused(self):
        """v3 単独なのに何も入力していないと1件も受信できない。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.mib_loading = False
        panel.mib_loaded = True
        panel.trap_version_combo.setCurrentText("v3")
        panel.trap_v3_username_edit.setText("")
        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
            panel._on_trap_start_clicked()
        warn.assert_called_once()
        panel.snmp_manager.start_trap_receiver.assert_not_called()

    def test_trap_both_without_a_v3_username_is_not_blocked(self):
        """v3 を使わない「両方」の利用者を、暗号方式の選択で止めないこと。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.snmp_manager.start_trap_receiver.return_value = True
        panel.mib_loading = False
        panel.mib_loaded = True
        panel.trap_version_combo.setCurrentText("両方")
        panel.trap_v3_username_edit.setText("")
        # v3 を使うつもりは無いが、暗号方式だけ触ってしまった状態
        panel.trap_v3_priv_combo.setCurrentIndex(
            [k for _l, k in panel.PRIV_PROTOCOL_CHOICES].index("AES-128"))
        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
            panel._on_trap_start_clicked()
        warn.assert_not_called()
        panel.snmp_manager.start_trap_receiver.assert_called_once()

    def test_trap_v3_rejects_an_unreadable_engine_id(self):
        """16進として読めない EngineID は具体的に指摘すること。"""
        from unittest import mock
        panel = self._panel()
        panel.snmp_manager = mock.Mock()
        panel.mib_loading = False
        panel.mib_loaded = True
        panel.trap_version_combo.setCurrentText("v3")
        panel.trap_v3_username_edit.setText("netbelt-v3")
        for bad in ("zzzz", "800000000102030"):   # 非16進 / 奇数桁
            with self.subTest(engine_id=bad):
                panel.trap_v3_engine_ids_edit.setPlainText(bad)
                panel.snmp_manager.reset_mock()
                with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
                    panel._on_trap_start_clicked()
                warn.assert_called_once()
                self.assertIn(bad, warn.call_args[0][2])
                panel.snmp_manager.start_trap_receiver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
