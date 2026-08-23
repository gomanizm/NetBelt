"""Syslog の UDP/TCP 同時受信と、稼働中の無停止での追加/削除を検証する。"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")


class SyslogDualProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import core.firewall as fw
        self._orig = fw.ensure_inbound_allow
        fw.ensure_inbound_allow = lambda *a, **k: (True, "test stub")  # UAC/実FWを叩かない
        from core.syslog_receiver import SyslogReceiver
        self.recv = SyslogReceiver()

    def tearDown(self):
        self.recv.stop()
        import core.firewall as fw
        fw.ensure_inbound_allow = self._orig

    def test_both_protocols_run_at_once(self):
        # port=0 で空きポートを使い、UDP と TCP を同時に起動できること
        self.assertTrue(self.recv.start_protocol("UDP", 0))
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        self.assertEqual(self.recv.active_protocols(), ["UDP", "TCP"])
        self.assertTrue(self.recv.is_protocol_running("UDP"))
        self.assertTrue(self.recv.is_protocol_running("TCP"))
        # それぞれ実ポートが取れる（状態表示に使う）
        self.assertIsInstance(self.recv.active_port("UDP"), int)
        self.assertIsInstance(self.recv.active_port("TCP"), int)

    def test_add_tcp_without_stopping_udp(self):
        # UDP 稼働中に TCP を追加しても、UDP のソケットは張り替えられない（無停止）
        self.assertTrue(self.recv.start_protocol("UDP", 0))
        udp_port = self.recv.active_port("UDP")
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        self.assertEqual(self.recv.active_port("UDP"), udp_port)  # UDP は無停止のまま
        # TCP だけ止めても UDP は動き続ける
        self.recv.stop_protocol("TCP")
        self.assertEqual(self.recv.active_protocols(), ["UDP"])
        self.assertEqual(self.recv.active_port("UDP"), udp_port)

    def test_udp_message_received_while_both_active(self):
        got = []
        self.recv.message_received.connect(lambda m: got.append(m))
        self.recv.start_protocol("UDP", 0)
        self.recv.start_protocol("TCP", 0)
        port = self.recv.active_port("UDP")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"<14>test udp message", ("127.0.0.1", port))
        s.close()
        for _ in range(20):
            self.app.processEvents()
            if got:
                break
            time.sleep(0.1)
        self.assertTrue(got, "UDP メッセージが受信されなかった")

    def test_tcp_message_received_while_both_active(self):
        got = []
        self.recv.message_received.connect(lambda m: got.append(m))
        self.recv.start_protocol("UDP", 0)
        self.recv.start_protocol("TCP", 0)
        port = self.recv.active_port("TCP")
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(b"<14>test tcp message\n")
        for _ in range(20):
            self.app.processEvents()
            if got:
                break
            time.sleep(0.1)
        c.close()
        self.assertTrue(got, "TCP メッセージが受信されなかった")

    def test_start_accepts_combined_protocol_string(self):
        self.assertTrue(self.recv.start(0, "UDP+TCP"))
        self.assertEqual(self.recv.active_protocols(), ["UDP", "TCP"])
        self.assertEqual(self.recv.protocol, "UDP+TCP")

    def test_separate_ports_per_protocol(self):
        # UDP と TCP で別ポートを指定できる
        self.assertTrue(self.recv.start_protocol("UDP", 0))
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        self.assertNotEqual(self.recv.active_port("UDP"), self.recv.active_port("TCP"))

    def test_message_carries_proto_and_port(self):
        # 受信メッセージに proto/port が載り、表示用文字列になる
        got = []
        self.recv.message_received.connect(lambda m: got.append(m))
        self.recv.start_protocol("UDP", 0)
        port = self.recv.active_port("UDP")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"<14>hello", ("127.0.0.1", port))
        s.close()
        for _ in range(20):
            self.app.processEvents()
            if got:
                break
            time.sleep(0.1)
        self.assertTrue(got)
        m = got[0]
        self.assertEqual(m.proto, "UDP")
        self.assertEqual(m.port, port)
        self.assertIn("(UDP/%d)" % port, m.source_display)


class SyslogPanelToggleTest(unittest.TestCase):
    """パネルのチェック操作でハングしない（存在しないメソッド呼び出しの回帰防止）"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_toggling_tcp_while_udp_runs_does_not_raise(self):
        from unittest import mock
        import core.firewall as fw
        from core.syslog_receiver import SyslogReceiver
        from ui.syslog_panel import SyslogPanel
        with mock.patch.object(fw, "ensure_inbound_allow", return_value=(True, "stub")):
            with mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
                p = SyslogPanel()
                recv = SyslogReceiver()
                p.set_syslog_receiver(recv)
                p.udp_port_spin.setValue(0)   # 空きポート
                p.tcp_port_spin.setValue(0)
                try:
                    p._toggle_receiver()                    # UDP 開始
                    self.assertEqual(recv.active_protocols(), ["UDP"])
                    p.tcp_check.setChecked(True)            # 稼働中に TCP 追加（旧実装はここで停止）
                    self.assertEqual(recv.active_protocols(), ["UDP", "TCP"])
                    p.tcp_check.setChecked(False)           # TCP だけ停止
                    self.assertEqual(recv.active_protocols(), ["UDP"])
                finally:
                    recv.stop()

if __name__ == "__main__":
    unittest.main()
