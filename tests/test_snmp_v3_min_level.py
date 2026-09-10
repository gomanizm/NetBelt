"""SNMPv3 Trap 受信が、登録したユーザより弱いセキュリティレベルの通知を捨てることを検証する。

pysnmp 5.1.0 の USM は、受信側が authoritative でない Trap では
「最低 securityLevel」の検査を行わない。authPriv で登録したユーザ名に対して、
鍵を付けない noAuthNoPriv の通知を送ると、そのまま受信コールバックへ届く
（実測: level=1 で受信・記録された）。ユーザ名と engineID は秘密ではないので、
鍵を知らない送信者が偽の Trap を一覧へ記録させられる。

登録時に求めたレベル（auth/priv の有無）より低い通知は捨て、一覧に載せない。
"""
import os
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

ENGINE_HEX = "8000000001020304"
USER = "nbuser"
AUTH = "authpass-12345"
PRIV = "privpass-12345"


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SnmpV3MinLevelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patcher = mock.patch("core.firewall.ensure_inbound_allow",
                             return_value=(True, "test stub"))
        patcher.start()
        self.addCleanup(patcher.stop)

    # 作った SNMPManager はクラス終了まで保持する（test_snmp_trap_receive.py と
    # 同じ理由: キューに残ったシグナルの配送先を先に捨てると落ちる）
    _managers = []

    @classmethod
    def tearDownClass(cls):
        for m in cls._managers:
            m.stop_trap_receiver()
        cls.app.processEvents()
        cls._managers.clear()

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.02)

    def _receiver(self, auth_protocol="SHA", priv_protocol="AES-128"):
        from core.snmp_manager import SNMPManager
        m = SNMPManager()
        self._managers.append(m)
        got = []
        m.trap_received.connect(got.append)
        port = free_udp_port()
        user = {"username": USER, "auth_protocol": auth_protocol,
                "auth_password": AUTH if auth_protocol != "none" else "",
                "priv_protocol": priv_protocol,
                "priv_password": PRIV if priv_protocol != "none" else "",
                "engine_ids": [ENGINE_HEX]}
        self.assertTrue(m.start_trap_receiver(port, ["public"], [user]))
        self._pump(0.6)
        return m, port, got

    @staticmethod
    def _send(user_data, port, label):
        from pysnmp.hlapi import (SnmpEngine, UdpTransportTarget, ContextData,
                                  NotificationType, ObjectIdentity, sendNotification)
        from pysnmp.proto.rfc1902 import OctetString
        engine = SnmpEngine(snmpEngineID=OctetString(hexValue=ENGINE_HEX))
        next(sendNotification(
            engine, user_data,
            UdpTransportTarget(("127.0.0.1", port), timeout=1, retries=0),
            ContextData(), "trap",
            NotificationType(ObjectIdentity("1.3.6.1.6.3.1.1.5.1")).addVarBinds(
                ("1.3.6.1.2.1.1.5.0", OctetString(label)))))

    def test_a_noauth_trap_for_an_authpriv_user_is_dropped(self):
        from pysnmp.hlapi import UsmUserData
        m, port, got = self._receiver()

        self._send(UsmUserData(USER), port, "forged")
        self._pump(1.0)

        self.assertEqual(got, [], "鍵なしの通知を受け入れている: %s" % got)

    def test_a_correct_authpriv_trap_is_still_received(self):
        from pysnmp.hlapi import (UsmUserData, usmHMACSHAAuthProtocol,
                                  usmAesCfb128Protocol)
        m, port, got = self._receiver()

        self._send(UsmUserData(USER, AUTH, PRIV,
                               authProtocol=usmHMACSHAAuthProtocol,
                               privProtocol=usmAesCfb128Protocol), port, "genuine")
        self._pump(1.0)

        self.assertEqual(len(got), 1, "正しい通知が届かない")
        self.assertEqual(got[0].get("security_level"), "3")

    def test_an_authnopriv_trap_for_an_authpriv_user_is_dropped(self):
        """認証はあっても暗号が無い通知は、authPriv 登録には足りない。"""
        from pysnmp.hlapi import (UsmUserData, usmHMACSHAAuthProtocol,
                                  usmNoPrivProtocol)
        m, port, got = self._receiver()

        self._send(UsmUserData(USER, AUTH, authProtocol=usmHMACSHAAuthProtocol,
                               privProtocol=usmNoPrivProtocol), port, "weaker")
        self._pump(1.0)

        self.assertEqual(got, [], "暗号なしの通知を受け入れている: %s" % got)

    def test_a_noauth_user_still_receives_noauth_traps(self):
        """noAuthNoPriv で登録したユーザには、これまでどおり鍵なしで届くこと（対照）。"""
        from pysnmp.hlapi import UsmUserData
        m, port, got = self._receiver(auth_protocol="none", priv_protocol="none")

        self._send(UsmUserData(USER), port, "plain")
        self._pump(1.0)

        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    unittest.main()
