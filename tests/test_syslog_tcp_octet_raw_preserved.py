"""octet-counting のフレームで raw_message が受信原文と一致することを確認する。

_emit_tcp_line が復号結果へ一律に .strip() をかけていたため、RFC 6587
§3.4.1 で長さが宣言されている本文であっても、末尾の空白・タブ・
NEL(U+0085)・NBSP(U+00A0) が raw_message から消えていた。宣言長ぶんは
そのまま本文なので、raw は原文のまま残さなければならない。

改行区切り（§3.4.2）は終端の CR/LF を落とす必要があるので、そちらの
挙動は変えない（下の 2 件目で固定する）。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")


def _frame(msg: bytes) -> bytes:
    return b"%d %s" % (len(msg), msg)


class SyslogTcpOctetRawPreservedTest(unittest.TestCase):
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
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        self.port = self.recv.active_port("TCP")

    def tearDown(self):
        self.recv.stop()
        import core.firewall as fw
        fw.ensure_inbound_allow = self._orig

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(5)
        c.connect(("127.0.0.1", self.port))
        return c

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_octet_counted_frame_keeps_trailing_whitespace_in_raw(self):
        body = ("<134>1 2026-09-09T10:00:00Z host app - - - trailing "
                "\t ")
        c = self._client()
        c.sendall(_frame(body.encode("utf-8")))
        self.assertTrue(self._wait(lambda: len(self.seen) >= 1), "届かない")
        self.assertEqual(self.seen[0].raw_message, body)

    def test_newline_framed_line_still_drops_its_crlf_terminator(self):
        c = self._client()
        c.sendall(b"<134>Sep  9 10:00:00 host newline framed\r\n")
        self.assertTrue(self._wait(lambda: len(self.seen) >= 1), "届かない")
        self.assertEqual(self.seen[0].raw_message,
                         "<134>Sep  9 10:00:00 host newline framed")


if __name__ == "__main__":
    unittest.main()
