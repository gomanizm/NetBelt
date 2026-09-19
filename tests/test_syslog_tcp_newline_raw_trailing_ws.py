"""TCP の改行区切りで、本文末尾の空白が raw_message から消えないこと。

_emit_tcp_line は octet-counting 以外の行へ decoded.strip() をかけていた。
LF は切り出しの時点で既に落ちているので、落とすべきは終端の CR だけなのに、
本文末尾の空白やタブまで消えていた。
実測: '<134>payload  \\t\\r\\n' と '<134>payload  \\t\\n' はどちらも
raw='<134>payload' になった（UDP と octet-counting では '<134>payload  \\t'）。

直し方: 改行区切りでは終端の CR を 1 個だけ落とす。先頭の空白は、PRI の
解釈を変えないよう従来どおり落とす。空行を捨てる判定は従来どおり。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")


class SyslogTcpNewlineRawTrailingWhitespaceTest(unittest.TestCase):
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

    def _send(self, data, expected_count):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(5)
        c.connect(("127.0.0.1", self.port))
        c.sendall(data)
        deadline = time.time() + 5
        while len(self.seen) < expected_count and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertEqual(len(self.seen), expected_count, "届かない")
        return [m.raw_message for m in self.seen]

    def test_crlf_line_keeps_trailing_whitespace_in_raw(self):
        raws = self._send(b"<134>payload  \t\r\n", 1)
        self.assertEqual(raws, ["<134>payload  \t"])

    def test_lf_line_keeps_trailing_whitespace_in_raw(self):
        raws = self._send(b"<134>payload  \t\n", 1)
        self.assertEqual(raws, ["<134>payload  \t"])

    def test_leading_whitespace_is_still_dropped_so_pri_is_parsed(self):
        raws = self._send(b"   <131>leading spaces\r\n", 1)
        self.assertEqual(raws, ["<131>leading spaces"])
        self.assertEqual(self.seen[0].severity, 3)

    def test_blank_lines_are_still_ignored(self):
        raws = self._send(b"\r\n  \t \r\n\n<134>after blanks\r\n", 1)
        self.assertEqual(raws, ["<134>after blanks"])


if __name__ == "__main__":
    unittest.main()
