"""シリアルポートを開いている最中の dispose() が、接続を取り消すことを検証する。

connect() は serial.Serial() が返るまで self.serial_conn が None のまま
なので、その間に dispose() が走っても閉じる相手が無い。そのうえ connect()
は生成後に _should_stop を無条件で False へ戻し、読み取りスレッドまで
起動して True を返す。タブを閉じた側（_on_tab_closed）は接続辞書から
del 済みなので、この後このポートを閉じる経路が無く、同じ COM への再接続は
Windows の排他で Access is denied になる。

USB-UART では数十 ms の窓だが、Bluetooth SPP のように開くのに数秒かかる
ポートでは現実に起きる。
"""
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


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


class SerialConnectCancelTest(unittest.TestCase):
    def _conn(self):
        from core.serial_connection import SerialConnection
        return SerialConnection("COM99", 9600)

    def test_a_dispose_while_the_port_is_opening_cancels_the_connection(self):
        """開いている途中に dispose されたら、開き終わったポートを閉じて失敗を返す。"""
        conn = self._conn()
        opened = []

        def open_port(*args, **kwargs):
            # ポートを開いている最中にタブが閉じられた
            conn.dispose()
            port = _IdlePort()
            opened.append(port)
            return port

        with mock.patch("core.serial_connection.serial.Serial",
                        side_effect=open_port):
            ok = conn.connect()

        port = opened[0]
        self.assertFalse(ok, "取り消されたのに接続成功を返している")
        self.assertTrue(port.closed.wait(1), "開き終わったポートを閉じていない")
        self.assertFalse(conn.is_connected)
        self.assertIsNone(conn.serial_conn, "閉じたポートへの参照が残っている")
        thread = conn._read_thread
        self.assertTrue(thread is None or not thread.is_alive(),
                        "取り消したのに読み取りスレッドが動いている")

    def test_dispose_joins_the_read_thread(self):
        """後始末で読み取りスレッドの終了を待つこと。"""
        conn = self._conn()
        with mock.patch("core.serial_connection.serial.Serial", _IdlePort):
            self.assertTrue(conn.connect())
        thread = conn._read_thread
        self.assertIsNotNone(thread, "読み取りスレッドを保持していない")
        self.assertTrue(thread.is_alive())

        conn.dispose()

        self.assertFalse(thread.is_alive(), "読み取りスレッドの終了を待っていない")

    def test_reconnecting_after_a_disconnect_still_works(self):
        """切断のあとに同じオブジェクトで繋ぎ直せること（取り消し判定の副作用が無い）。"""
        conn = self._conn()
        with mock.patch("core.serial_connection.serial.Serial", _IdlePort):
            self.assertTrue(conn.connect())
            conn.disconnect()
            self.assertTrue(conn.connect(), "切断後の再接続が取り消し扱いになっている")
            self.assertTrue(conn.is_connected)
            conn.dispose()


if __name__ == "__main__":
    unittest.main()
