"""connect() が動き出す前の dispose() が、後から成立する接続を取り消すことを検証する。

タブを閉じたときの MainWindow は、接続辞書から参照を捨ててから dispose() を
呼ぶ。接続は別スレッドで始まるので、スレッドが connect() の本体に入る前に
dispose() が着地することがある。このとき connect() は入口で後始末の印
（_should_stop）を無条件に False へ戻してしまうため、印が消え、COM ポートを
開いたまま True を返す。開いたポートを参照しているものは誰もいないので閉じる
経路が無く、Windows の COM は同一プロセス内でも排他なので、同じ機器への再接続
はアプリを再起動するまで Access is denied になる。

開いている最中の dispose() は既に塞がれている（tests/test_serial_connect_cancel.py）。
ここで見るのは、その手前、connect() がまだ何も始めていない時刻に dispose() が
着地した場合。
"""
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core.serial_connection import SerialConnection       # noqa: E402


class _IdlePort:
    """開いているが何も送ってこないポート。"""

    def __init__(self, *args, **kwargs):
        self.is_open = True
        self.in_waiting = 0
        self.closed = threading.Event()

    def read(self, n):
        return b""

    def close(self):
        self.is_open = False
        self.closed.set()


class SerialDisposeBeforeConnectTest(unittest.TestCase):
    def _connect_after_a_dispose(self):
        """connect() を呼ぶ前にタブを閉じ、そのあと接続スレッドを走らせる。

        戻り値は (SerialConnection, connect() の戻り値, 開かれたポートの並び)。
        """
        conn = SerialConnection("COM99", 9600)
        opened = []

        def open_port(*args, **kwargs):
            port = _IdlePort()
            opened.append(port)
            return port

        # 接続スレッドが動き出す前にタブが閉じられた
        conn.dispose()

        with mock.patch("core.serial_connection.serial.Serial",
                        side_effect=open_port):
            returned = conn.connect()

        self.addCleanup(conn.dispose)
        return conn, returned, opened

    def test_a_connect_after_a_dispose_is_cancelled(self):
        """破棄済みの接続では、接続を成立させずに失敗を返すこと。"""
        conn, returned, opened = self._connect_after_a_dispose()

        self.assertFalse(
            returned,
            "破棄済みなのに接続成功を返している（開いたポート数: %d）" % len(opened))
        self.assertFalse(conn.is_connected, "破棄済みなのに接続済みになっている")

    def test_a_connect_after_a_dispose_leaves_no_open_port(self):
        """破棄済みの接続では、開いたままの COM ポートを残さないこと。"""
        conn, _, opened = self._connect_after_a_dispose()

        for port in opened:
            self.assertFalse(
                port.is_open,
                "破棄済みなのに COM ポートが開いたまま残っている")
        self.assertIsNone(conn.serial_conn, "ポートへの参照が残っている")

    def test_a_connect_after_a_dispose_starts_no_read_thread(self):
        """破棄済みの接続では、読み取りスレッドを起こさないこと。"""
        conn, _, _ = self._connect_after_a_dispose()

        thread = conn._read_thread
        self.assertTrue(
            thread is None or not thread.is_alive(),
            "破棄済みなのに読み取りスレッドが動いている: %r" % (thread,))

    def test_the_dispose_mark_survives_the_entry_of_connect(self):
        """後始末の印を connect() の入口で消さないこと。"""
        conn = SerialConnection("COM99", 9600)
        conn.dispose()
        with mock.patch("core.serial_connection.serial.Serial", _IdlePort):
            conn.connect()
        self.assertTrue(
            conn._should_stop,
            "connect() の入口で後始末の印が消されている")


if __name__ == "__main__":
    unittest.main()
