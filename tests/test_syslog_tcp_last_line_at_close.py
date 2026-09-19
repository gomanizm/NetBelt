"""TCP の改行区切りで、LF の無い最後の 1 行を送ってから閉じても、その行が届くこと。

_handle_tcp_client は recv が空（相手が閉じた）になると、受信バッファに
残った分を配信せずにループを抜けていた。改行区切りでは LF が来るまで
行を配信しないので、最後の行に LF を付けずに閉じる送り手（1 件だけ送って
閉じるスクリプトや、終端を付けない実装）の最後の 1 件が黙って消えた。
実測: b"<134>first\\n<134>last" を送って閉じると、届いたのは
'<134>first' の 1 件だけだった。

直し方: 相手が閉じた時点でバッファに残っている改行区切りの行も 1 件として
配信する。長さは受信のたびに上限（max_line_bytes）と照合済みなので、
上限を超えた行はここまで来ない。宣言した長さに足りないまま閉じた
octet-counting のフレームは、欠けた本文なので従来どおり配信しない。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")


class SyslogTcpLastLineAtCloseTest(unittest.TestCase):
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

    def _send_and_close(self, data, expected_count, settle=0.0):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(("127.0.0.1", self.port))
        c.sendall(data)
        c.close()
        deadline = time.time() + 5
        while len(self.seen) < expected_count and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        end = time.time() + settle
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.02)
        return [m.raw_message for m in self.seen]

    def test_last_line_without_newline_is_delivered_when_the_peer_closes(self):
        raws = self._send_and_close(b"<134>first\n<134>last", 2)
        self.assertEqual(raws, ["<134>first", "<134>last"],
                         "LF の無い最後の行が閉じたときに捨てられた")

    def test_a_single_unterminated_message_is_delivered(self):
        raws = self._send_and_close(b"<131>only one, no newline", 1)
        self.assertEqual(raws, ["<131>only one, no newline"])
        self.assertEqual(self.seen[0].severity, 3)

    def test_a_trailing_cr_is_dropped_as_part_of_crlf(self):
        raws = self._send_and_close(b"<134>cut between cr and lf\r", 1)
        self.assertEqual(raws, ["<134>cut between cr and lf"])

    def test_nothing_extra_when_the_last_line_was_terminated(self):
        raws = self._send_and_close(b"<134>a\n<134>b\n", 2, settle=0.5)
        self.assertEqual(raws, ["<134>a", "<134>b"])

    def test_a_truncated_octet_counted_frame_is_still_not_delivered(self):
        raws = self._send_and_close(b"14 <134>complete!100 <134>cut short", 1,
                                    settle=0.5)
        self.assertEqual(raws, ["<134>complete!"])


if __name__ == "__main__":
    unittest.main()
