"""GUI への配送待ちが青天井に積み上がらないことを確認する。

max_messages(1000) が効くのは GUI が受け取った後だけで、受信スレッドから
GUI へ渡す Qt のキューには上限が無い。障害時のフラップ程度の短時間の
バーストでも、GUI の処理能力（実測 約540件/秒）を超えたぶんがすべて
キューに残り、GUI が長時間固まる。認証の要らない LAN ホストから起こせる。

実測: イベントループを回さずに 200 件送ると、200 件すべてが配送待ちとして
残り、回し始めると 200 件すべてが GUI へ流れ込む。

受信側で配送待ちの件数を数え、上限を超える間は捨てる。捨てた件数は
はけた時点で「N 件を取りこぼしました」として 1 件だけ流す。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")

CAP = 10
BURST = 200


class SyslogPendingQueueCapTest(unittest.TestCase):
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

    def _start_tcp(self):
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(5)
        c.connect(("127.0.0.1", self.recv.active_port("TCP")))
        return c

    def _wait_received(self, count, seconds=15.0):
        """GUI のイベントループを回さずに、受信スレッドの処理を待つ"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.recv.message_count >= count:
                return True
            time.sleep(0.05)
        return False

    def _drain(self, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            before = len(self.seen)
            self.app.processEvents()
            if len(self.seen) == before:
                return
            time.sleep(0.01)

    def test_there_is_a_default_cap_on_undelivered_messages(self):
        self.assertTrue(hasattr(self.recv, "max_pending_messages"),
                        "配送待ちの上限が無い")
        self.assertGreater(self.recv.max_pending_messages, 0)

    def test_a_burst_does_not_pile_up_while_the_gui_is_busy(self):
        self.recv.max_pending_messages = CAP
        c = self._start_tcp()
        for i in range(BURST):
            c.sendall(b"<134>Sep  9 10:00:00 rtr01 burst %d\n" % i)
        self.assertTrue(self._wait_received(BURST),
                        "受信スレッドが %d 件を処理しない (message_count=%d)"
                        % (BURST, self.recv.message_count))
        self.assertEqual(len(self.seen), 0,
                         "イベントループを回していないのに配送された")

        self._drain()
        # 上限ぶん + 取りこぼし通知 1 件までしか溜まっていないこと
        self.assertLessEqual(
            len(self.seen), CAP + 1,
            "配送待ちが上限を超えて積み上がった: %d 件" % len(self.seen))
        self.assertGreater(self.recv.dropped_message_count, 0,
                           "取りこぼしが数えられていない")

    def test_the_dropped_count_is_reported_once(self):
        self.recv.max_pending_messages = CAP
        c = self._start_tcp()
        for i in range(BURST):
            c.sendall(b"<134>Sep  9 10:00:00 rtr01 burst %d\n" % i)
        self.assertTrue(self._wait_received(BURST))
        self._drain()
        notices = [m for m in self.seen if "取りこぼ" in m.message]
        self.assertEqual(len(notices), 1,
                         "取りこぼしの通知が 1 件ではない: %r"
                         % [m.message for m in self.seen])
        self.assertIn(str(self.recv.dropped_message_count), notices[0].message)

    def test_traffic_below_the_cap_is_never_dropped(self):
        self.recv.max_pending_messages = CAP
        c = self._start_tcp()
        for i in range(5):
            c.sendall(b"<134>Sep  9 10:00:00 rtr01 quiet %d\n" % i)
        self.assertTrue(self._wait_received(5))
        self._drain()
        self.assertEqual(sum(1 for m in self.seen if "quiet" in m.message), 5,
                         "上限に達していないのに落とされた: %r"
                         % [m.message for m in self.seen])
        self.assertEqual(self.recv.dropped_message_count, 0)


if __name__ == "__main__":
    unittest.main()
