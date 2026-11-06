# -*- coding: utf-8 -*-
"""終端を送らず少しずつ送り続ける TCP 接続が、無通信タイムアウトを免れないこと。

無通信の判定が「最後に recv が何か返してから」の経過だけだったので、改行を
付けずに 1 バイトずつ送り続ける接続は永久に切られず、同時接続の上限ぶん
張られると正規の機器の TCP syslog が accept 直後に閉じられて届かなくなった。
判定は「完全なメッセージが最後に届いてから」の経過で行う。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")


class SyslogTcpTrickleIdleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.syslog_receiver import SyslogReceiver
        self.recv = SyslogReceiver()
        self.seen = []
        self.recv.message_received.connect(self.seen.append)
        self.addCleanup(self.recv.stop)

    def _start(self):
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        self.port = self.recv.active_port("TCP")

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

    @staticmethod
    def _trickle_until_closed(c, seconds):
        """1 バイトずつ 0.3 秒おきに送り、サーバーに閉じられたら True"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                c.sendall(b"A")
            except OSError:
                return True
            try:
                c.settimeout(0.3)
                if c.recv(1) == b"":
                    return True
            except socket.timeout:
                continue
            except OSError:
                return True
        return False

    def test_a_trickling_connection_is_closed_by_the_idle_timeout(self):
        self.recv.max_tcp_connections = 1
        self.recv.tcp_idle_timeout_seconds = 1.5
        self._start()
        trickle = self._client()
        self.assertTrue(self._wait(lambda: len(self.recv.tcp_clients) >= 1))
        self.assertTrue(self._trickle_until_closed(trickle, seconds=8.0),
                        "終端を送らない接続が無通信タイムアウトで閉じられない")
        self.assertTrue(self._wait(
            lambda: sum(1 for t in self.recv.tcp_clients if t.is_alive()) < 1,
            seconds=5.0), "枠が返ってこない")
        good = self._client()
        good.sendall(b"<134>Sep  9 10:00:00 host after trickle\n")
        self.assertTrue(self._wait(lambda: any("after trickle" in m.message
                                               for m in self.seen)),
                        "遅延接続が枠を占有したまま")

    def test_complete_messages_keep_the_connection_open(self):
        """完全なメッセージを送り続ける接続は切られないこと（従来どおり）"""
        self.recv.tcp_idle_timeout_seconds = 1.5
        self._start()
        c = self._client()
        for i in range(4):
            c.sendall(b"<134>Sep  9 10:00:00 host beat %d\n" % i)
            time.sleep(0.9)
        self.assertTrue(self._wait(lambda: sum(1 for m in self.seen
                                               if "beat" in m.message) == 4))

    def test_a_message_split_across_sends_is_delivered(self):
        """1 件が数回に分かれて届いても、時間内なら配信されること"""
        self.recv.tcp_idle_timeout_seconds = 3
        self._start()
        c = self._client()
        for part in (b"<134>Sep  9 10:00:00 ", b"host split ", b"message\n"):
            c.sendall(part)
            time.sleep(0.5)
        self.assertTrue(self._wait(lambda: any("split message" in m.message
                                               for m in self.seen)))


if __name__ == "__main__":
    unittest.main()
