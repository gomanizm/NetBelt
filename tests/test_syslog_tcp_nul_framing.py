"""TCP で NUL 終端のメッセージ（非透過フレーミングの終端に NUL を使う送り手）も届くこと。

TCP の改行区切りは LF だけを終端として扱っていた。RFC 6587 3.4.2 は
非透過フレーミングの終端に NUL が使われてきたことに触れており、Python 標準の
logging.handlers.SysLogHandler(socktype=SOCK_STREAM) は各メッセージの末尾に
NUL を付けて送る（LF は付けない）。
実測: SysLogHandler で 3 件送ると、接続を開いている間は 1 件も届かなかった
（LF が来ないので 1 行が伸び続け、上限の 64KiB に達すると接続ごと切られる）。

直し方: 改行区切りの終端に NUL も受け付け、LF と NUL の早い方で区切る。
NUL の探索は次の LF の手前までに限る（長い受信バッファを行ごとに
末尾まで走査しない）。1 行の長さの上限は、終端までの長さに同じように効く。
"""
import logging
import logging.handlers
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")


class SyslogTcpNulFramingTest(unittest.TestCase):
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

    def _wait(self, expected_count, settle=0.0):
        deadline = time.time() + 5
        while len(self.seen) < expected_count and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        end = time.time() + settle
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.02)
        return [m.raw_message for m in self.seen]

    def _open(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(5)
        c.connect(("127.0.0.1", self.port))
        return c

    def test_python_sysloghandler_over_tcp_is_received(self):
        handler = logging.handlers.SysLogHandler(
            address=("127.0.0.1", self.port), socktype=socket.SOCK_STREAM)
        self.addCleanup(handler.close)
        logger = logging.getLogger("netbelt-test-syslog-nul-%d" % id(self))
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        for text in ("one", "two", "three"):
            logger.warning(text)
        # 接続は開いたまま（SysLogHandler は送るたびに閉じない）
        raws = self._wait(3)
        self.assertEqual(raws, ["<12>one", "<12>two", "<12>three"],
                         "NUL 終端のメッセージが届かない")
        self.assertEqual(self.seen[0].severity, 4)

    def test_nul_and_lf_terminators_can_be_mixed(self):
        c = self._open()
        c.sendall(b"<134>a\x00<134>b\n<134>c\x00\n<134>d\r\x00")
        raws = self._wait(4, settle=0.3)
        self.assertEqual(raws, ["<134>a", "<134>b", "<134>c", "<134>d"])

    def test_a_nul_split_across_receives_still_ends_the_message(self):
        c = self._open()
        c.sendall(b"<134>first half")
        time.sleep(0.2)
        c.sendall(b" second half\x00<134>next\x00")
        raws = self._wait(2)
        self.assertEqual(raws, ["<134>first half second half", "<134>next"])

    def test_the_line_limit_still_applies_to_nul_framed_messages(self):
        self.recv.max_line_bytes = 32
        c = self._open()
        c.sendall(b"<134>" + b"x" * 27 + b"\x00")       # ちょうど 32 バイト
        c.sendall(b"<134>" + b"y" * 40 + b"\x00")       # 上限超過で切断
        raws = self._wait(2, settle=0.3)
        self.assertEqual(raws[0], "<134>" + "x" * 27)
        self.assertEqual(len(raws), 2)
        self.assertIn("切断", raws[1])


if __name__ == "__main__":
    unittest.main()
