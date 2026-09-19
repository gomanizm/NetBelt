"""終端の無い最後の 1 行が、相手が閉じなかった場合でも捨てられないこと。

TCP の改行区切りは終端（LF / NUL）が来るまで行を配信しない。残りを
配信するのは recv が空（相手が閉じた）になった枝だけだったので、
終端を付けずに黙った送り手の最後の 1 件は、接続が無通信タイムアウトで
切られた場合と、サーバを停止した場合に消えたままだった。
実測（'<134>terminated\\n' に続けて終端なしの '<134>no-terminator' を
送る）: 相手が閉じた場合だけ 2 件届き、無通信タイムアウトとサーバ停止では
1 件しか届かなかった。

直し方: 後始末で残りを配信する処理を 1 か所にまとめ、無通信タイムアウトで
切る枝と、停止要求で待受ループを抜けた経路からも通す。1 行が上限を
超えて切断した枝だけは対象外にする（切り捨てたことを通知した直後に、
その切れ端を 1 件として出してしまうため）。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")

TERMINATED = b"<134>terminated\n"
RESIDUAL = b"<134>no-terminator"


class SyslogTcpResidualPathsTest(unittest.TestCase):
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

    def _listen(self):
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        return self.recv.active_port("TCP")

    def _connect(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(("127.0.0.1", self._listen()))
        self.addCleanup(c.close)
        return c

    def _wait(self, count, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline and len(self.seen) < count:
            self.app.processEvents()
            time.sleep(0.02)
        self.app.processEvents()
        return [m.raw_message for m in self.seen]

    def test_the_idle_timeout_still_delivers_the_unterminated_line(self):
        self.recv.tcp_idle_timeout_seconds = 1
        c = self._connect()
        c.sendall(TERMINATED)
        c.sendall(RESIDUAL)          # 終端を付けずに黙る

        raws = self._wait(2, 10)
        self.assertEqual(raws, ["<134>terminated", "<134>no-terminator"],
                         "無通信タイムアウトで切るときに捨てられた: %r" % (raws,))

    def test_stopping_the_server_still_delivers_the_unterminated_line(self):
        c = self._connect()
        c.sendall(TERMINATED)
        c.sendall(RESIDUAL)
        self._wait(1, 5)             # 終端付きの 1 件目が届くまで待つ

        self.recv.stop()             # 相手は閉じていない
        raws = self._wait(2, 3)
        self.assertEqual(raws, ["<134>terminated", "<134>no-terminator"],
                         "停止のときに捨てられた: %r" % (raws,))

    def test_a_line_cut_for_being_too_long_is_not_delivered_afterwards(self):
        self.recv.max_line_bytes = 32
        c = self._connect()
        c.sendall(b"<134>" + b"x" * 200)     # 終端なしで上限を超える

        raws = self._wait(1, 5)
        self._wait(2, 1.0)                   # 追加で出ないことを見る
        self.assertEqual(len(raws), 1,
                         "切り捨てた行が後から配信された: %r" % (raws,))
        self.assertIn("切断しました", raws[0])

    def test_a_terminated_last_line_is_not_delivered_twice(self):
        c = self._connect()
        c.sendall(TERMINATED)
        self._wait(1, 5)

        self.recv.stop()
        raws = self._wait(2, 1.0)
        self.assertEqual(raws, ["<134>terminated"],
                         "残りが無いのに配信された: %r" % (raws,))


if __name__ == "__main__":
    unittest.main()
