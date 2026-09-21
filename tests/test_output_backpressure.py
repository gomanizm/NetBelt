"""描き待ちが増えすぎたら、受信スレッドに読むのを止めさせることを検証する。

何が起きていたか（検証役の実測: scratchpad\\cx5j-verify-termui\\
t06d_localhost_flood.py、offscreen、繋いだのは 127.0.0.1 のみ）:
127.0.0.1 の TCP サーバーが 80 桁の行を流し続け、本物の TelnetConnection で
受けて queue_output へ渡し、Qt のイベントループを 27.5 秒回した。

    t= 2.5s 受信済み 10.1 MB 描き待ち  1,282,048 文字 ( 1.2 MB)
    t=10.0s 受信済み 35.2 MB 描き待ち  4,878,336 文字 ( 4.7 MB)
    t=17.5s 受信済み 60.7 MB 描き待ち  9,252,864 文字 ( 8.8 MB)
    t=25.0s 受信済み 88.8 MB 描き待ち 20,267,008 文字 (19.3 MB)
    t=27.5s 受信済み 97.4 MB 描き待ち 22,347,776 文字 (21.3 MB)

頭打ちにならず単調に増える。受信側への流量制御が無く、_PendingOutput にも
queue_output にも上限が無いため、描くのが追いつかない間じゅう溜まり続ける。

利用者の決定（2026-09-20）: 受信を待たせる。描き待ちが上限を超えている間は
受信スレッドがソケットから読むのを止め、TCP のウィンドウで機器側を待たせる
（取りこぼしを出さない＝記録の全量を守る）。上限は通常の操作で待たせが
起きない大きさにする。

どう直したか: TerminalWidget が機器ごとに「関所」(threading.Event) を持ち、
描き待ちが PENDING_HIGH_WATER を超えたら閉じ、PENDING_LOW_WATER まで
減ったら開ける。SSH / Telnet の受信ループは関所が閉じている間 recv を
呼ばない。読まないでいると OS（SSH は paramiko のチャネル窓）の受信
バッファが埋まり、機器側が送るのを待つ。
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")


class CountingSocket:
    """recv を呼ばれた回数を数えるだけの偽ソケット。"""

    def __init__(self):
        self.calls = 0

    def recv(self, size):
        self.calls += 1
        time.sleep(0.005)
        return b"x" * 64


class CountingChannel:
    """paramiko のチャネルの、受信ループが使う部分だけの偽物。"""

    closed = False

    def __init__(self):
        self.calls = 0

    def recv_ready(self):
        return True

    def recv(self, size):
        self.calls += 1
        time.sleep(0.005)
        return b"x" * 64


class OutputBackpressureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(self._discard, w)
        w.create_terminal_tab("dev")
        return w

    @staticmethod
    def _discard(w):
        w._output_timer.stop()
        w._pending_output.clear()
        w.close()

    @staticmethod
    def _fill(w, upto):
        """描き待ちが upto 文字を超えるまで受信させる。"""
        chunk = "x" * 4096
        while len(w._pending_output.get("dev", ())) <= upto:
            w.queue_output("dev", chunk)

    def test_the_gate_closes_once_the_backlog_passes_the_limit(self):
        """描き待ちが上限を超えたら、受信を止める合図を出すこと。"""
        w = self._widget()
        gate = w.output_gate("dev")
        self.assertTrue(gate.is_set(), "前提: 最初は読んでよい")

        self._fill(w, w.PENDING_HIGH_WATER)

        self.assertFalse(gate.is_set(),
                         "描き待ちが %d 文字を超えても受信を止めなかった"
                         % w.PENDING_HIGH_WATER)

    def test_the_gate_opens_again_once_the_backlog_is_drawn(self):
        """描き終えて減ったら、受信を再開させること。"""
        w = self._widget()
        gate = w.output_gate("dev")
        self._fill(w, w.PENDING_HIGH_WATER)
        self.assertFalse(gate.is_set(), "前提: 止めている")

        while w._pending_output.get("dev") and not gate.is_set():
            w._flush_pending_output()

        self.assertTrue(gate.is_set(), "描き終えても受信を再開しなかった")
        self.assertLessEqual(len(w._pending_output.get("dev", ())),
                             w.PENDING_LOW_WATER,
                             "止めた直後の量で再開していて、止め直しを繰り返す")

    def test_the_limit_is_above_a_normal_burst(self):
        """上限は、通常の操作で届く量より十分大きいこと。

        1 コマンドの出力で最大級の show tech-support でも数 MB。実測した
        描画速度は色付きで 2.07 MB/s、80 桁の行で 4〜5.6 MB/s なので、
        4 MiB 以上あれば普通の操作では上限に触れない。
        """
        from ui.terminal_widget import TerminalWidget
        self.assertGreaterEqual(TerminalWidget.PENDING_HIGH_WATER, 4 << 20)
        self.assertLess(TerminalWidget.PENDING_LOW_WATER,
                        TerminalWidget.PENDING_HIGH_WATER)

    def _run_until_stopped(self, conn, counter):
        """受信ループを別スレッドで回し、終わりを後始末に登録する。"""
        thread = threading.Thread(target=conn._read_output, daemon=True)

        def stop():
            conn._stop_reading = True
            conn.is_connected = False
            thread.join(5)

        self.addCleanup(stop)
        thread.start()
        return thread

    @staticmethod
    def _wait_for(predicate, seconds=5.0):
        end = time.time() + seconds
        while time.time() < end and not predicate():
            time.sleep(0.01)
        return predicate()

    def test_a_telnet_session_stops_reading_while_the_gate_is_closed(self):
        """関所が閉じている間、Telnet はソケットから読まないこと。"""
        from core.telnet_connection import TelnetConnection
        conn = TelnetConnection("192.0.2.10", 23)
        sock = CountingSocket()
        conn.socket = sock
        conn.is_connected = True
        gate = threading.Event()        # 閉じたまま渡す
        conn.set_read_gate(gate)

        self._run_until_stopped(conn, sock)
        time.sleep(0.2)
        self.assertEqual(sock.calls, 0,
                         "止めているのにソケットから読んだ（溜まり続ける）")

        gate.set()
        self.assertTrue(self._wait_for(lambda: sock.calls > 0),
                        "再開させても読み始めなかった")

    def test_an_ssh_session_stops_reading_while_the_gate_is_closed(self):
        """関所が閉じている間、SSH はチャネルから読まないこと。

        読まないでおくと paramiko がチャネルの窓を広げないので、機器側は
        送るのを待つ（捨てられない）。
        """
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.10", 22, "u")
        channel = CountingChannel()
        conn.channel = channel
        conn.is_connected = True
        gate = threading.Event()
        conn.set_read_gate(gate)

        self._run_until_stopped(conn, channel)
        time.sleep(0.2)
        self.assertEqual(channel.calls, 0,
                         "止めているのにチャネルから読んだ（溜まり続ける）")

        gate.set()
        self.assertTrue(self._wait_for(lambda: channel.calls > 0),
                        "再開させても読み始めなかった")


if __name__ == "__main__":
    unittest.main()
