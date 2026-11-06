# -*- coding: utf-8 -*-
"""v1 Trap の v1→v2 変換に失敗しても、pysnmp の受信側の控えが残らないことと、
Trap の待ち受けで受信バッファを広げることを検証する。

pysnmp（7.1.28〜7.1.31）の ntfrcv.process_pdu は先頭で rfc2576.v1_to_v2 を
呼ぶ。varbind の INTEGER が Integer32 の範囲を超えている（2147483648）と
ValueConstraintError、generic-trap が 0〜6 以外だと KeyError になり、
rfc3412.receive_message の processPdu の後ろにある後始末
（del self.__transportInfo[stateReference]）が飛ぶ（try/finally でない）。
壊れた v1 Trap 1 件ごとに 1 エントリ（約 300 B）残り、受信を止めるまで
増え続けた（実測: 2,000 件で +2,000）。
"""
import socket
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")

from conftest import free_udp_port   # noqa: E402


def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag, body):
    return bytes([tag]) + _ber_len(len(body)) + body


def _ber_int(value, tag=0x02):
    length = max(1, (value.bit_length() + 8) // 8)
    return _tlv(tag, value.to_bytes(length, "big", signed=True))


def _ber_oid(parts):
    out = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.insert(0, 0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(chunk)
    return _tlv(0x06, out)


def _v1_trap(generic=6, value=None, community=b"public"):
    """手組みの v1 Trap（RFC 5612 の企業番号 32473 の下）"""
    if value is None:
        value = _ber_int(1)
    vb = _tlv(0x30, _ber_oid([1, 3, 6, 1, 4, 1, 32473, 9, 0]) + value)
    pdu = _tlv(0xA4, _ber_oid([1, 3, 6, 1, 4, 1, 32473])
               + _tlv(0x40, bytes([127, 0, 0, 1]))
               + _ber_int(generic) + _ber_int(1) + _ber_int(12345, tag=0x43)
               + _tlv(0x30, vb))
    return _tlv(0x30, _ber_int(0) + _tlv(0x04, community) + pdu)


class SnmpTrapV1ConversionFailureTest(unittest.TestCase):
    _managers = []

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        for m in cls._managers:
            m.stop_trap_receiver()
        QApplication.processEvents()
        cls._managers.clear()

    def setUp(self):
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _start(self):
        from core.snmp_manager import SNMPManager
        m = SNMPManager()
        self._managers.append(m)
        self.addCleanup(m.stop_trap_receiver)
        port = free_udp_port()
        self.assertTrue(m.start_trap_receiver(port, ["public"]))
        deadline = time.time() + 5
        while time.time() < deadline:
            if m.trap_receiver and m.trap_receiver.isRunning():
                break
            time.sleep(0.05)
        return m, port

    def _pump(self, condition, timeout=5):
        from PyQt6.QtWidgets import QApplication
        deadline = time.time() + timeout
        while not condition() and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        return condition()

    def test_broken_v1_traps_leave_no_transport_info(self):
        m, port = self._start()
        got = []
        m.trap_received.connect(got.append)
        info = (m.trap_receiver._engine.message_dispatcher
                ._MsgAndPduDispatcher__transportInfo)

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        for packet in (_v1_trap(value=_ber_int(2147483648)), _v1_trap(generic=7)):
            for _ in range(20):
                sender.sendto(packet, ("127.0.0.1", port))
                time.sleep(0.002)
        # 後ろに正常な Trap を送り、それが届いた時点で前の分も処理済み
        sender.sendto(_v1_trap(), ("127.0.0.1", port))
        self.assertTrue(self._pump(lambda: got), "正常な v1 Trap が届かない")
        self.assertEqual(len(info), 0, "変換に失敗した Trap の控えが残っている")
        self.assertTrue(m.trap_receiver.isRunning())

    def test_the_receive_buffer_is_enlarged(self):
        from core.snmp_manager import SNMPTrapReceiver
        m, _ = self._start()
        sock = m.trap_receiver._socket
        self.assertGreaterEqual(
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
            SNMPTrapReceiver.RECEIVE_BUFFER_BYTES)
        self.assertEqual(SNMPTrapReceiver.RECEIVE_BUFFER_BYTES, 1 << 20)


if __name__ == "__main__":
    unittest.main()
