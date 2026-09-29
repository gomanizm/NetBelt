"""SSH の書き手のスレッドを、打鍵・貼り付けの区切りごとに作り直さないことを検証する。

何が起きていたか（41cac56 で測った。localhost の paramiko サーバ、鍵交換なし）。
41cac56 は、判定の直後に鍵交換が始まっても GUI を止めないよう、チャネルへの
書き込みを書き手のスレッド（_ChannelWriter）へ出した。書き手は渡されるたびに
スレッドを作り、書き終えると終わっていたので、打鍵 1 回は 31b45ee の平均
0.04 ms から 0.22 ms へ、端末経由の 540 KB の貼り付けは 0.62 秒から 1.03 秒へ
（約 1.7 倍）遅くなった（512 文字の区切りごとに約 0.4 ms）。

どう直したか。書き終えたスレッドは、次の受け渡しを短い間（_LINGER_SECONDS）
待ってから終わる。その間に渡された物は同じスレッドが書く。止めれば
（dispose・繋ぎ直し）、待っているスレッドもすぐ終わる。直したあとの同じ
測定は、打鍵 0.07〜0.09 ms、貼り付け 0.62〜0.64 秒。

スレッドの本数を数えるテストは、待ち（_LINGER_SECONDS）を 30 秒にしてから
数える。既定の 1 秒のままだと、混んだ機械でテストのスレッドが受け渡しの間に
1 秒を超えて止まったとき、書き手は正しく終わって次の受け渡しで 2 本目が
始まり、『1 != 2』で落ちていた（1d9d5c5 で、受け渡しの呼び出し側を 10 回目の
前に 1.3 秒止めると、数える 2 件とも落ちた）。待ちを長くしても、受け渡しごとに
スレッドを作れば本数で、待っている書き手を起こし損ねれば書き終わりの待ちで
落ちる。ただし待ちを差し替えるので、本体の既定の待ちが 0 に戻る退行（冒頭の
遅さへ戻る）は本数では捕まらない（数える 2 件の待ちを 30 秒にし、既定の待ちを
確かめる件がまだ無かった版で、本体の既定を 0 にしても、このファイルの 6 件は
すべて通った）。既定の待ちは
test_default_linger_outlasts_back_to_back_hand_overs で、停止に左右されない
よう値そのものを確かめる。

数える 2 件が書き終わりを待つ上限は 5 秒にした。待ちが 1 秒だった頃に、
それより短くするために決めた 0.5 秒のままだと、混んだ機械で書き手の側が
0.5 秒を超えて止まったとき、本体は正しく書き終えるのに『書き終わらない』で
落ちていた（上限が 0.5 秒だった版で、偽のチャネルの 12 回目の書き込みを
0.7 秒止めると、数える 2 件とも落ちた）。待ちは 30 秒なので、書き手を
起こし損ねれば 5 秒の上限で落ちる。
"""
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

WRITER_THREAD_NAME = "netbelt-channel-writer"


class _FakeTransport:
    def __init__(self, sock):
        self.sock = sock
        self.clear_to_send = threading.Event()
        self.clear_to_send.set()


class _RecordingChannel:
    """書いた順に writes へ控えるチャネル（偽物。待たずに書ける）"""

    closed = False
    out_window_size = 1024 * 1024

    def __init__(self, transport):
        self.transport = transport
        self.writes = []

    def get_transport(self):
        return self.transport

    def sendall(self, data):
        self.writes.append(("data", bytes(data)))

    def resize_pty(self, width, height):
        self.writes.append(("size", (width, height)))

    def close(self):
        self.closed = True


class _WriterThreads:
    """書き手のスレッドの開始を数える（ほかのスレッドはそのまま始める）"""

    def __init__(self):
        self.started = []
        self._real_start = threading.Thread.start

    def __enter__(self):
        real_start = self._real_start
        started = self.started

        def counting_start(thread):
            if thread.name == WRITER_THREAD_NAME:
                started.append(thread)
            return real_start(thread)

        self._patcher = mock.patch.object(threading.Thread, "start",
                                          counting_start)
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


class _ChannelCase(unittest.TestCase):
    def _channel(self):
        a, b = socket.socketpair()      # 待たずに書ける（詰めていない）
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return _RecordingChannel(_FakeTransport(a))

    def _writer(self, channel):
        from core.ssh_connection import _ChannelWriter
        writer = _ChannelWriter(channel)
        self.addCleanup(writer.stop)
        return writer


class WriterKeepsItsThreadTest(_ChannelCase):
    def test_default_linger_outlasts_back_to_back_hand_overs(self):
        """本体の既定の待ちが、続けて渡される区切りの間より十分に長い"""
        from core.ssh_connection import _ChannelWriter
        # 貼り付けの区切りは続けて渡される（冒頭の直したあとの測定では、
        # 540 KB を 512 文字ずつ 0.62〜0.64 秒で書いた。1 区切りあたり
        # 1 ms 未満）。その 100 倍を下限にする
        linger = _ChannelWriter._LINGER_SECONDS
        self.assertGreaterEqual(
            linger, 0.1,
            "既定の待ちが %.3f 秒しかない。続けて渡される区切りの間より短いと、"
            "受け渡しごとに書き手のスレッドを作る" % linger)

    def test_consecutive_hand_overs_use_one_thread(self):
        from core.ssh_connection import _ChannelWriter
        channel = self._channel()
        writer = self._writer(channel)
        expected = []
        # 待ちを長くする（テストのスレッドが受け渡しの間に止まっても数え違えない）
        with mock.patch.object(_ChannelWriter, "_LINGER_SECONDS", 30.0), \
                _WriterThreads() as threads:
            for i in range(50):
                data = b"chunk%d" % i
                # 待っている書き手を起こし損ねると、待ちの終わり（30 秒）まで
                # 書かれない。それより十分短く、書き手の側が混んで止まるより
                # 長く待つ
                self.assertTrue(writer.write(data, 5.0),
                                "書き込みが終わらない（%d 個目）" % i)
                expected.append(("data", data))
                if i % 10 == 9:
                    writer.send_size(80 + i, 24, 5.0)
                    expected.append(("size", (80 + i, 24)))

        self.assertEqual(expected, channel.writes, "渡した順に書かれていない")
        self.assertEqual(1, len(threads.started),
                         "渡すたびに書き手のスレッドを作り直している（%d 本）"
                         % len(threads.started))

    def test_stop_ends_the_waiting_thread_at_once(self):
        from core.ssh_connection import _ChannelWriter
        channel = self._channel()
        writer = self._writer(channel)
        with mock.patch.object(_ChannelWriter, "_LINGER_SECONDS", 30.0), \
                _WriterThreads() as threads:
            self.assertTrue(writer.write(b"x", 2.0))
            self.assertEqual(1, len(threads.started), "前提: 書き手のスレッドが無い")
            thread = threads.started[0]

            started = time.perf_counter()
            writer.stop()
            thread.join(2.0)
            elapsed = time.perf_counter() - started

        self.assertFalse(thread.is_alive(), "止めたのに、書き手のスレッドが残っている")
        self.assertLess(elapsed, 1.0, "止めてから書き手が終わるまで %.2f 秒" % elapsed)
        self.assertTrue(writer.write(b"y", 0.2), "止めたあとの受け渡しが待たされた")
        self.assertEqual([("data", b"x")], channel.writes, "止めたあとに書いた")

    def test_a_thread_left_waiting_ends_and_the_next_hand_over_starts_one(self):
        from core.ssh_connection import _ChannelWriter
        channel = self._channel()
        writer = self._writer(channel)
        with mock.patch.object(_ChannelWriter, "_LINGER_SECONDS", 0.05), \
                _WriterThreads() as threads:
            self.assertTrue(writer.write(b"a", 2.0))
            first = threads.started[0]
            first.join(2.0)
            self.assertFalse(first.is_alive(),
                             "次が来ないのに、書き手のスレッドが終わらない")
            self.assertFalse(writer.busy())

            self.assertTrue(writer.write(b"b", 2.0),
                            "スレッドが終わったあとの受け渡しが書かれない")

        self.assertEqual([("data", b"a"), ("data", b"b")], channel.writes)
        self.assertEqual(2, len(threads.started))

    def test_hand_overs_while_the_wait_runs_out_are_not_lost(self):
        """待ち終わりと受け渡しが重なっても、渡した物は必ず書かれる"""
        from core.ssh_connection import _ChannelWriter
        channel = self._channel()
        writer = self._writer(channel)
        expected = []
        with mock.patch.object(_ChannelWriter, "_LINGER_SECONDS", 0.002):
            for i in range(300):
                data = b"%d," % i
                self.assertTrue(writer.write(data, 2.0),
                                "受け渡しが書かれないまま残った（%d 個目）" % i)
                expected.append(("data", data))
                time.sleep((i % 4) * 0.001)

        self.assertEqual(expected, channel.writes)


class _QtCase(_ChannelCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _session(self, channel):
        """connect() と同じ見張りを持つ SSHConnection に、偽のチャネルを差す"""
        from core.send_backpressure import DrainWatcher
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.channel = channel
        conn.is_connected = True
        conn._drain_watcher = DrainWatcher(
            lambda: conn._send_backlogged(channel), conn._announce_drained)
        self.addCleanup(conn.dispose)
        return conn

    def _pump(self, seconds, until):
        end = time.time() + seconds
        while time.time() < end and not until():
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()


class TypingKeepsTheThreadTest(_QtCase):
    def test_typing_does_not_start_a_thread_per_key(self):
        from core.ssh_connection import _ChannelWriter
        channel = self._channel()
        conn = self._session(channel)
        # 待ちを長くする（テストのスレッドが打鍵の間に止まっても数え違えない）
        with mock.patch.object(_ChannelWriter, "_LINGER_SECONDS", 30.0), \
                _WriterThreads() as threads:
            for ch in "show running-config\r":
                conn.send_command(ch)
                # ふだんは渡したその場で書き終わる。混んだ機械でも、待っている
                # 書き手を起こし損ねたとき（待ちの終わりの 30 秒まで書かれない）
                # より十分短い間（5 秒）に書き終わる
                self._pump(5.0, until=lambda: not conn.has_pending_sends())
                self.assertFalse(conn.has_pending_sends(),
                                 "打鍵 %r が書き終わらない" % ch)
            conn.set_terminal_size(132, 43)
            self._pump(5.0, until=lambda: len(channel.writes) > 20)

        self.assertEqual(
            [("data", ch.encode()) for ch in "show running-config\r"]
            + [("size", (132, 43))], channel.writes)
        self.assertEqual(1, len(threads.started),
                         "打鍵ごとに書き手のスレッドを作っている（%d 本）"
                         % len(threads.started))

    def test_dispose_ends_the_waiting_thread(self):
        from core.ssh_connection import _ChannelWriter
        channel = self._channel()
        conn = self._session(channel)
        with mock.patch.object(_ChannelWriter, "_LINGER_SECONDS", 30.0), \
                _WriterThreads() as threads:
            conn.send_command("x")
            self.assertEqual(1, len(threads.started), "前提: 書き手のスレッドが無い")
            self._pump(2.0, until=lambda: channel.writes)
            conn.dispose()
            threads.started[0].join(2.0)

        self.assertFalse(threads.started[0].is_alive(),
                         "後始末のあとも、書き手のスレッドが残っている")
        self.assertEqual([("data", b"x")], channel.writes)


if __name__ == "__main__":
    unittest.main()
