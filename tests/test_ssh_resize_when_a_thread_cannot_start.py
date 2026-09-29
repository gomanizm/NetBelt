"""書き手のスレッドを作れなくても、端末の大きさが黙って落ちないことを検証する。

何が起きていたか（30a3821 で確かめた。fix/v1.3.2 では同じ変更が 6c90f50。
以下は当時の設計の説明）。window-change の書き込みは、GUI スレッドの外
（window-change だけを書く送り役のスレッド）へ出した。送り役は書くたびに
スレッドを始めるが、プロセスがスレッドを作れない（threading.Thread.start が
RuntimeError）と、渡された大きさは誰にも書かれずに残った。呼び出し元は
「送った」扱いにしているので、次に大きさが変わるまで、機器側の端末の
大きさは古いままだった（エラーも出ない）。送り役を入れる前は、その場で
書いていた。
  - 書ける状態（鍵交換中でも TCP が詰まってもいない）で、GUI スレッドの
    スレッドの開始だけを失敗させて set_terminal_size(100, 30): 偽のチャネルの
    resize_pty は 1 回も呼ばれない

どう直したか。スレッドを作れないときは、これまでどおりその場（GUI
スレッド）で書く。いまは、データも大きさも同じ書き手（_ChannelWriter）が
書き、書き終えたスレッドは次の受け渡しを少し待つ。書き手がスレッドを
始めるのは待っているスレッドが無いときだけで、そこで始められなければ、
同じくその場で書く。
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


class _RecordingChannel:
    """resize_pty を記録するだけのチャネル（偽物。待たずに書ける）"""

    closed = False
    out_window_size = 1024 * 1024

    def __init__(self, transport):
        self.transport = transport
        self.resizes = []

    def get_transport(self):
        return self.transport

    def resize_pty(self, width, height):
        self.resizes.append((width, height))

    def close(self):
        self.closed = True


_real_start = threading.Thread.start


def _start_fails_on_the_gui_thread(thread):
    """GUI スレッドからのスレッドの開始だけを失敗させる（ほかのスレッドには効かせない）"""
    if threading.current_thread() is threading.main_thread():
        raise RuntimeError("can't start new thread")
    return _real_start(thread)


class ThreadCannotStartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _session(self):
        """connect() と同じ見張りを持つ SSHConnection に、偽のチャネルを差す"""
        from core.send_backpressure import DrainWatcher
        from core.ssh_connection import SSHConnection
        a, b = socket.socketpair()      # 待たずに書ける（詰めていない）
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        channel = _RecordingChannel(_FakeTransport(a))
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.channel = channel
        conn.is_connected = True
        conn._drain_watcher = DrainWatcher(
            lambda: conn._send_backlogged(channel), conn._announce_drained)
        self.addCleanup(conn.dispose)
        return conn, channel

    def test_the_size_is_written_in_place_when_no_thread_can_start(self):
        conn, channel = self._session()

        with mock.patch.object(threading.Thread, "start",
                               _start_fails_on_the_gui_thread):
            conn.set_terminal_size(100, 30)
        self._pump(0.3)

        self.assertEqual([(100, 30)], channel.resizes,
                         "スレッドを作れないと、端末の大きさが機器へ届かない")

    def test_the_next_size_still_goes_out_after_the_failure(self):
        """失敗のあとも書き手が「書いている最中」に残らず、次の大きさを書く"""
        conn, channel = self._session()
        with mock.patch.object(threading.Thread, "start",
                               _start_fails_on_the_gui_thread):
            conn.set_terminal_size(100, 30)

        conn.set_terminal_size(120, 40)
        self._pump(0.3)

        self.assertEqual((120, 40), channel.resizes[-1] if channel.resizes else None,
                         "スレッドを作れなかったあと、次の大きさが届かない")


if __name__ == "__main__":
    unittest.main()
