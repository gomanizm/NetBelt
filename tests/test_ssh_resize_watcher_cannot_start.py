"""保留した端末の大きさの見張りを始められなくても、打鍵が止まらないことを検証する。

何が起きていたか（48e8d27 で実測）。48e8d27 で、保留した大きさ（_size_unsent）が
ある間はデータも「待たずには書けない」（_send_backlogged が True）として扱い、
大きさの見張り（_size_watcher の DrainWatcher）が大きさを書き手へ渡すまで
打鍵を待たせるようにした（window-change を打鍵に追い越させない）。ところが
その見張りのスレッドを作れない（threading.Thread.start が RuntimeError）と、
DrainWatcher.check は開始できなかったスレッドを「見張り中」の印に残したまま
例外を投げ、_size_unsent は True のまま残った。以後、見張りは知らせを出さず、
データはずっと待たされた（知らせも出ない。次に端末の大きさが変わるまで）。
  - 偽のチャネル・鍵交換中に、GUI スレッドからの見張りの開始だけを失敗させて
    set_terminal_size(132, 43)、send_command('x')、鍵交換を終わらせて 3 秒:
    機器へは何も届かず、has_pending_sends は True のまま
  - 同じ失敗のあと、次の鍵交換中の set_terminal_size(120, 40) も、残った印の
    せいで見張りが始まらず、送られなかった
  - 同じ失敗のあとの dispose は、開始できなかったスレッドを join しようとして
    RuntimeError になり、チャネルを閉じずに抜けていた
  （78ef376 は、データが大きさを待たないので打鍵は届いたが、大きさはその
  打鍵のあとに届いていた）
その場で書き手へ渡す直し方も試したが、書き手もスレッドを作れないと GUI
スレッドで書くことになり、鍵交換が終わるまで GUI が止まった（localhost の
paramiko サーバで、1 秒の鍵交換の間 set_terminal_size が 1.0 秒戻らなかった）。

どう直したか。大きさの見張りを始められないときは、開始に失敗した見張りを捨て、
GUI スレッドのタイマーで（見張りと同じ間隔で）調べ直す（予約はいつも 1 本だけ。
その間に大きさが何度変わっても増やさない）。書けるようになったら、
いつもどおり大きさを書き手へ渡し、そのあとで打鍵が続く。保留している間は
これまでどおり打鍵を待たせるので、追い越されない。GUI は待たない。
"""
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _FakeTransport:
    def __init__(self, sock):
        self.sock = sock
        self.clear_to_send = threading.Event()
        self.clear_to_send.set()


class _OrderRecordingChannel:
    """書いた順に記録するチャネル（偽物）

    paramiko と同じく、鍵交換中（clear_to_send が解除されている間）は、
    書けるようになるまで戻らない（ここでは最大 10 秒）。
    """

    closed = False
    out_window_size = 1024 * 1024

    def __init__(self, transport):
        self.transport = transport
        self.calls = []

    def get_transport(self):
        return self.transport

    def resize_pty(self, width, height):
        self.transport.clear_to_send.wait(10)
        self.calls.append(("resize", width, height))

    def sendall(self, data):
        self.transport.clear_to_send.wait(10)
        self.calls.append(("data", bytes(data)))

    def close(self):
        self.closed = True


_real_start = threading.Thread.start


def _drain_watcher_start_fails_on_the_gui_thread(thread):
    """GUI スレッドからの見張り（netbelt-send-drain）の開始だけを失敗させる"""
    if (threading.current_thread() is threading.main_thread()
            and thread.name == "netbelt-send-drain"):
        raise RuntimeError("can't start new thread")
    return _real_start(thread)


class SizeWatcherCannotStartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _session(self):
        """connect() と同じ見張りを持つ SSHConnection に、偽のチャネルを差す"""
        from core.send_backpressure import DrainWatcher
        from core.ssh_connection import SSHConnection
        a, b = socket.socketpair()      # TCP へは待たずに書ける
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        channel = _OrderRecordingChannel(_FakeTransport(a))
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.channel = channel
        conn.is_connected = True
        conn._drain_watcher = DrainWatcher(
            lambda: conn._send_backlogged(channel), conn._announce_drained)
        self.addCleanup(conn.dispose)
        # 書き手が偽のチャネルで待ち続けないように、先に鍵交換を終わらせる
        self.addCleanup(channel.transport.clear_to_send.set)
        return conn, channel

    def _resize_while_the_watcher_cannot_start(self, conn, channel):
        """鍵交換中に、大きさの見張りを始められない状態で set_terminal_size(132, 43)"""
        channel.transport.clear_to_send.clear()   # 鍵交換中
        with mock.patch.object(threading.Thread, "start",
                               _drain_watcher_start_fails_on_the_gui_thread):
            try:
                conn.set_terminal_size(132, 43)
            except RuntimeError:
                pass   # アプリでは excepthook が記録して続ける（main.install_excepthook）

    def _has_data(self, channel):
        return any(call[0] == "data" for call in channel.calls)

    def test_typing_follows_the_size_when_its_watcher_cannot_start(self):
        conn, channel = self._session()
        self._resize_while_the_watcher_cannot_start(conn, channel)

        conn.send_command("x")
        channel.transport.clear_to_send.set()     # 鍵交換が終わる
        self._pump(3.0, until=lambda: self._has_data(channel))

        self.assertEqual([("resize", 132, 43), ("data", b"x")], channel.calls,
                         "見張りを始められないと、打鍵が届かないか、"
                         "window-change より先に届く")
        self.assertFalse(conn.has_pending_sends(),
                         "見張りを始められなかったあと、端末が待たされたまま")

    def test_a_later_held_size_still_goes_out_before_typing(self):
        """失敗した見張りが残らず、次に保留した大きさも打鍵より先に送られる"""
        conn, channel = self._session()
        self._resize_while_the_watcher_cannot_start(conn, channel)
        channel.transport.clear_to_send.set()
        self._pump(0.3)

        channel.transport.clear_to_send.clear()   # 次の鍵交換
        conn.set_terminal_size(120, 40)
        conn.send_command("y")
        channel.transport.clear_to_send.set()
        self._pump(3.0, until=lambda: self._has_data(channel))

        self.assertEqual([("resize", 120, 40), ("data", b"y")], channel.calls[-2:],
                         "見張りを始められなかったあと、次に保留した大きさが"
                         "送られないか、打鍵のあとに届く")

    def test_the_gui_does_not_wait_for_the_key_exchange_when_no_thread_can_start(self):
        """見張りも書き手もスレッドを作れなくても、鍵交換中に GUI スレッドで書いて止めない"""
        conn, channel = self._session()
        channel.transport.clear_to_send.clear()   # 鍵交換中

        def start(thread):
            if (threading.current_thread() is threading.main_thread()
                    and thread.name in ("netbelt-send-drain",
                                        "netbelt-channel-writer")):
                raise RuntimeError("can't start new thread")
            return _real_start(thread)
        with mock.patch.object(threading.Thread, "start", start):
            began = time.monotonic()
            try:
                conn.set_terminal_size(132, 43)
            except RuntimeError:
                pass   # アプリでは excepthook が記録して続ける（main.install_excepthook）
            took = time.monotonic() - began
            self._pump(0.3)
            waited = time.monotonic() - began

            channel.transport.clear_to_send.set()  # 鍵交換が終わる
            self._pump(3.0, until=lambda: channel.calls)

        # 偽のチャネルは、鍵交換中に書くと 10 秒まで戻らない
        self.assertLess(took, 1.0,
                        "鍵交換中の set_terminal_size が、書けるまで GUI を止めた")
        self.assertLess(waited, 2.0,
                        "鍵交換中に、GUI スレッドが大きさを書いて止まった")
        self.assertEqual([("resize", 132, 43)], channel.calls,
                         "スレッドを作れないと、鍵交換のあとに大きさが届かない")

    def test_repeated_resizes_keep_a_single_retry(self):
        """見張りを始められない間に何度大きさが変わっても、調べ直しは 1 本だけ

        b103c45 では、失敗のたびにタイマーの連鎖が 1 本ずつ増え、100 回の
        set_terminal_size で Transport を毎秒約 7,000 回調べ続けた（鍵交換が
        終わるまで）。調べ直しの間隔は見張りと同じ（POLL_SECONDS）なので、
        1 本なら window 秒の間に調べるのは window / POLL_SECONDS 回ほど。
        """
        from core.send_backpressure import DrainWatcher
        conn, channel = self._session()
        polls = [0]
        get_transport = channel.get_transport

        def counting_get_transport():
            polls[0] += 1
            return get_transport()
        channel.get_transport = counting_get_transport

        channel.transport.clear_to_send.clear()   # 鍵交換中
        with mock.patch.object(threading.Thread, "start",
                               _drain_watcher_start_fails_on_the_gui_thread):
            for i in range(100):
                try:
                    conn.set_terminal_size(80 + i, 24)
                except RuntimeError:
                    pass   # アプリでは excepthook が記録して続ける
                self.app.processEvents()
            polls[0] = 0
            window = 0.3
            self._pump(window)
            polled = polls[0]

            channel.transport.clear_to_send.set()  # 鍵交換が終わる
            self._pump(3.0, until=lambda: channel.calls)
            self._pump(0.1)
            polls[0] = 0
            self._pump(0.3)
            polled_after = polls[0]

        limit = int(window / DrainWatcher.POLL_SECONDS) + 10
        self.assertLessEqual(polled, limit,
                             "見張りを始められない間、大きさが変わるたびに"
                             "調べ直しの連鎖が増えた")
        self.assertEqual([("resize", 179, 24)], channel.calls,
                         "鍵交換のあと、最後の大きさが 1 回だけ届かない")
        self.assertEqual(0, polled_after,
                         "大きさを送ったあとも、調べ直しが続いている")

    def test_dispose_closes_the_channel_after_the_watcher_failed(self):
        conn, channel = self._session()
        self._resize_while_the_watcher_cannot_start(conn, channel)
        channel.transport.clear_to_send.set()
        self._pump(0.3)

        conn.dispose()

        self.assertTrue(channel.closed,
                        "見張りを始められなかったあと、後始末でチャネルが閉じない")


if __name__ == "__main__":
    unittest.main()
