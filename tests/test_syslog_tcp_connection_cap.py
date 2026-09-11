"""TCP の同時接続数に上限があり、超過分は accept 直後に閉じられることを確認する。

_run_tcp_loop は accept したぶんだけ無制限にスレッドを起こしていた。
実測: 何も送らないアイドル接続を 300 本張ると threads=302 / tcp_clients=300。
待受は 0.0.0.0 なので LAN 上の任意ホストから資源枯渇を引き起こせる。

TFTP の max_workers と同様に上限（既定 64）を設け、超過分は閉じる。
閉じられた接続が減れば、また受け付ける。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")


class SyslogTcpConnectionCapTest(unittest.TestCase):
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
    def _closed_by_server(c, seconds=3.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                c.settimeout(0.3)
                return c.recv(1) == b""
            except socket.timeout:
                continue
            except OSError:
                return True
        return False

    def test_there_is_a_default_cap(self):
        self.assertTrue(hasattr(self.recv, "max_tcp_connections"),
                        "同時接続数の上限が無い")
        self.assertGreater(self.recv.max_tcp_connections, 0)

    def test_connections_beyond_the_cap_are_closed(self):
        self.recv.max_tcp_connections = 3
        self._start()
        kept = [self._client() for _ in range(3)]
        self.assertTrue(self._wait(lambda: len(self.recv.tcp_clients) >= 3))
        extra = self._client()
        self.assertTrue(self._closed_by_server(extra),
                        "上限を超えた接続が閉じられない")
        # 既存の接続はそのまま使える
        kept[0].sendall(b"<134>Sep  9 10:00:00 host still alive\n")
        self.assertTrue(self._wait(lambda: any("still alive" in m.message
                                               for m in self.seen)))
        self.assertLessEqual(len(self.recv.tcp_clients), 3)

    def test_a_slot_freed_by_disconnect_is_reused(self):
        self.recv.max_tcp_connections = 2
        self._start()
        a = self._client()
        b = self._client()
        self.assertTrue(self._wait(lambda: len(self.recv.tcp_clients) >= 2))
        a.close()
        self.assertTrue(self._wait(
            lambda: sum(1 for t in self.recv.tcp_clients if t.is_alive()) < 2))
        c = self._client()
        c.sendall(b"<134>Sep  9 10:00:00 host reused slot\n")
        self.assertTrue(self._wait(lambda: any("reused slot" in m.message
                                               for m in self.seen)),
                        "空いた枠で受け付けない")
        b.close()


    def test_an_idle_connection_does_not_hold_a_slot_forever(self):
        """1 バイトも送らない接続が、枠を占有し続けないこと。

        上限だけを入れると、アイドル接続を上限ぶん張るだけで正規の機器の
        syslog が永久に届かなくなる（recv がタイムアウトしても continue
        するだけで、接続は切れない）。
        """
        self.recv.max_tcp_connections = 1
        self.recv.tcp_idle_timeout_seconds = 1.5
        self._start()
        idle = self._client()
        self.assertTrue(self._wait(lambda: len(self.recv.tcp_clients) >= 1))

        self.assertTrue(self._closed_by_server(idle, seconds=10.0),
                        "無通信の接続が閉じられない")
        self.assertTrue(self._wait(
            lambda: sum(1 for t in self.recv.tcp_clients if t.is_alive()) < 1,
            seconds=10.0), "枠が返ってこない")

        good = self._client()
        good.sendall(b"<134>Sep  9 10:00:00 host after idle\n")
        self.assertTrue(self._wait(lambda: any("after idle" in m.message
                                               for m in self.seen)),
                        "アイドル接続が枠を占有したまま")

    def test_a_sending_connection_is_not_dropped_by_the_idle_timeout(self):
        """送り続けている接続は、無通信タイムアウトで切られないこと。"""
        self.recv.tcp_idle_timeout_seconds = 2.0
        self._start()
        c = self._client()
        for i in range(4):
            c.sendall(b"<134>Sep  9 10:00:00 host beat %d\n" % i)
            time.sleep(0.9)
        self.assertTrue(self._wait(lambda: sum(1 for m in self.seen
                                               if "beat" in m.message) == 4),
                        "送信中の接続が切られている: %r"
                        % [m.message for m in self.seen])


if __name__ == "__main__":
    unittest.main()
