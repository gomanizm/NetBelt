"""CRLF で終わる行の長さを、LF の行と同じ基準で測ることを検証する。

1 行の長さは「最初の LF までのバイト数」で見ている。CRLF を使う送信元
（Windows 系のエージェントや一部の機器）では、その数に CR が含まれる。
CR は配信前に strip で落とすので中身には入らないのに、上限の判定にだけ
数えられ、中身がちょうど上限の行が LF なら通り CRLF なら切られる。

1 バイトの境界の話だが、同じ中身で終端の違いだけで切られるのは
筋が通らない。LF の直前の CR は数えない。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")

PRI = b"<134>"


class SyslogCrlfAtLimitTest(unittest.TestCase):
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
        self.limit = self.recv.max_line_bytes

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

    def _line(self, content_len, terminator):
        """PRI を含めた中身がちょうど content_len バイトの行。"""
        return PRI + b"A" * (content_len - len(PRI)) + terminator

    def _was_cut(self):
        return any("切断" in m.message for m in self.seen)

    def test_a_crlf_line_whose_content_is_exactly_the_limit_is_accepted(self):
        c = self._client()
        c.sendall(self._line(self.limit, b"\r\n"))
        self.assertTrue(self._wait(lambda: len(self.seen) >= 1),
                        "行が届かない")
        self.assertFalse(self._was_cut(),
                         "中身は上限ちょうどなのに、CR を数えて切っている")

    def test_an_lf_line_whose_content_is_exactly_the_limit_is_accepted(self):
        """LF ではこれまでも通っていた（同じ基準であることの対照）。"""
        c = self._client()
        c.sendall(self._line(self.limit, b"\n"))
        self.assertTrue(self._wait(lambda: len(self.seen) >= 1))
        self.assertFalse(self._was_cut())

    def test_a_crlf_line_one_byte_over_the_limit_is_still_refused(self):
        """緩めすぎていないこと。"""
        c = self._client()
        c.sendall(self._line(self.limit + 1, b"\r\n"))
        self.assertTrue(self._wait(self._was_cut),
                        "上限を超えた行を通している")


if __name__ == "__main__":
    unittest.main()
