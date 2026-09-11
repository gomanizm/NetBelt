"""非 UTF-8 バイトを含む syslog が黙って欠落せず latin-1 で復元されることを確認する。

受信側は data.decode("utf-8", errors="ignore") としていたため、UTF-8 として
不正なバイト（例: latin-1 の \\xe9）は本文からも raw からも消え、except 節の
latin-1 フォールバックには到達しなかった。実測: 送信 'caf\\xe9 done' →
message='caf done' raw='<134>... caf done'。

UTF-8 を strict でまず試し、失敗したら latin-1 へ落とす（1 バイトも欠けない）。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")

LATIN1_LINE = b"<134>Sep  9 10:00:00 host caf\xe9 done"
UTF8_LINE = "<134>Sep  9 10:00:00 host 日本語 done".encode("utf-8")


class SyslogDecodeFallbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import core.firewall as fw
        self._orig = fw.ensure_inbound_allow
        fw.ensure_inbound_allow = lambda *a, **k: (True, "test stub")
        from core.syslog_receiver import SyslogReceiver
        self.recv = SyslogReceiver()
        self.seen = []
        self.recv.message_received.connect(self.seen.append)

    def tearDown(self):
        self.recv.stop()
        import core.firewall as fw
        fw.ensure_inbound_allow = self._orig

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_udp_non_utf8_byte_falls_back_to_latin1(self):
        self.assertTrue(self.recv.start_protocol("UDP", 0))
        port = self.recv.active_port("UDP")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.sendto(LATIN1_LINE, ("127.0.0.1", port))
        self.assertTrue(self._wait(lambda: len(self.seen) >= 1), "届かない")
        m = self.seen[0]
        self.assertEqual(m.message, "café done",
                         "非 UTF-8 バイトが欠落した: %r" % m.message)
        self.assertIn("café done", m.raw_message)

    def test_udp_valid_utf8_is_still_decoded_as_utf8(self):
        self.assertTrue(self.recv.start_protocol("UDP", 0))
        port = self.recv.active_port("UDP")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.sendto(UTF8_LINE, ("127.0.0.1", port))
        self.assertTrue(self._wait(lambda: len(self.seen) >= 1), "届かない")
        self.assertEqual(self.seen[0].message, "日本語 done")

    def test_tcp_non_utf8_byte_falls_back_to_latin1(self):
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        port = self.recv.active_port("TCP")
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(5)
        c.connect(("127.0.0.1", port))
        c.sendall(LATIN1_LINE + b"\n")
        self.assertTrue(self._wait(lambda: len(self.seen) >= 1), "届かない")
        m = self.seen[0]
        self.assertEqual(m.message, "café done",
                         "非 UTF-8 バイトが欠落した: %r" % m.message)
        self.assertIn("café done", m.raw_message)


if __name__ == "__main__":
    unittest.main()
