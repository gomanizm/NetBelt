"""TCP 受信が RFC 6587 §3.4.1 の octet-counting フレーミングを受け付けることを確認する。

_handle_tcp_client は改行区切り（§3.4.2 non-transparent framing）しか
見ておらず、"<len> <msg>" 形式のフレームは改行が無いので一度も配信されず、
切断で黙って破棄されていた。実測: 2 フレーム送って 1.5 秒後 seen=0、
切断後も seen=0。syslog-ng の syslog() ドライバや rsyslog の
octet-counted 設定など実在するクライアントで起きる。

バッファ先頭が "<数字> <" なら長さ分を切り出す。改行区切りはそのまま。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")


def _frame(msg: bytes) -> bytes:
    return b"%d %s" % (len(msg), msg)


class SyslogTcpOctetCountingTest(unittest.TestCase):
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

    def test_two_octet_counted_frames_in_one_send_are_both_delivered(self):
        c = self._client()
        c.sendall(_frame(b"<134>1 2026-09-09T10:00:00Z host app - - - first")
                  + _frame(b"<134>1 2026-09-09T10:00:00Z host app - - - second"))
        self.assertTrue(self._wait(lambda: len(self.seen) >= 2),
                        "octet-counting のフレームが届かない: %d" % len(self.seen))
        self.assertEqual([m.message for m in self.seen[:2]], ["first", "second"])

    def test_a_frame_split_across_two_sends_is_reassembled(self):
        c = self._client()
        data = _frame(b"<134>1 2026-09-09T10:00:00Z host app - - - split body")
        c.sendall(data[:12])
        time.sleep(0.3)
        c.sendall(data[12:])
        self.assertTrue(self._wait(lambda: len(self.seen) >= 1), "届かない")
        self.assertEqual(self.seen[0].message, "split body")

    def test_a_frame_whose_body_contains_a_newline_is_one_message(self):
        c = self._client()
        c.sendall(_frame(b"<134>1 2026-09-09T10:00:00Z host app - - - line1\nline2"))
        self.assertTrue(self._wait(lambda: len(self.seen) >= 1), "届かない")
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0].message, "line1\nline2")

    def test_newline_framing_still_works_and_can_mix(self):
        c = self._client()
        c.sendall(b"<134>Sep  9 10:00:00 host newline framed\n"
                  + _frame(b"<134>Sep  9 10:00:00 host counted")
                  + b"<134>Sep  9 10:00:00 host newline again\n")
        self.assertTrue(self._wait(lambda: len(self.seen) >= 3),
                        "混在が処理できない: %s" % [m.message for m in self.seen])
        self.assertEqual([m.message for m in self.seen[:3]],
                         ["newline framed", "counted", "newline again"])

    def test_an_overlong_octet_count_is_refused_and_recorded(self):
        limit = self.recv.max_line_bytes
        c = self._client()
        c.sendall(b"%d <134>" % (limit + 1000))
        self.assertTrue(
            self._wait(lambda: any("切断" in m.message for m in self.seen)),
            "上限を超える長さ宣言を受け入れている")


if __name__ == "__main__":
    unittest.main()
