"""シリアルへの送信が GUI スレッドを止めないことを検証する。

send_command は GUI スレッドから呼ばれ、write と flush を同期で実行して
いた。pyserial の write は送り切るまで待ち、flush は送信完了を待つので、
ドライバの送信バッファを除けばほぼ線路時間だけ GUI が止まる。
512 文字の貼り付け 1 チャンクで、9600 baud では約 0.5 秒、300 baud では
約 17 秒（計測: 50 ms タイマーの最大間隔 17.11 秒）。

送信をワーカースレッドとキューへ移し、呼び出し側はすぐ戻るようにする。
順序はキューが保つ。
"""
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")


class _SlowPort:
    """write に線路時間ぶんの遅れがある疑似ポート。"""

    def __init__(self, delay):
        self.delay = delay
        self.is_open = True
        self.in_waiting = 0
        self.writes = []
        self.writer_threads = []
        self.written = threading.Event()

    def write(self, data):
        time.sleep(self.delay)
        self.writes.append(bytes(data))
        self.writer_threads.append(threading.current_thread())
        self.written.set()
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        return b""

    def close(self):
        self.is_open = False


class SerialSendOffGuiThreadTest(unittest.TestCase):
    def _conn(self, port):
        from core.serial_connection import SerialConnection
        conn = SerialConnection("COM99", 9600)
        conn.serial_conn = port
        conn._is_connected = True
        conn._should_stop = False
        return conn

    def test_send_command_returns_before_the_write_completes(self):
        port = _SlowPort(delay=0.5)
        conn = self._conn(port)
        payload = "x" * 512

        started = time.monotonic()
        conn.send_command(payload)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2,
                        "送信が呼び出し側（GUI スレッド）を %.2f 秒止めた" % elapsed)
        self.assertTrue(port.written.wait(3), "送信が届いていない")
        self.assertEqual(port.writes, [payload.encode("utf-8")])
        self.assertNotIn(threading.current_thread(), port.writer_threads,
                         "呼び出し側のスレッドで書いている")
        conn.dispose()

    def test_sends_arrive_in_order(self):
        port = _SlowPort(delay=0.01)
        conn = self._conn(port)
        for ch in "abcde":
            conn.send_command(ch)
        deadline = time.monotonic() + 3
        while len(port.writes) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(b"".join(port.writes), b"abcde", "順序が崩れている")
        conn.dispose()

    def test_dispose_stops_the_writer_thread(self):
        port = _SlowPort(delay=0.0)
        conn = self._conn(port)
        conn.send_command("a")
        self.assertTrue(port.written.wait(3))
        thread = port.writer_threads[0]

        conn.dispose()

        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "後始末で送信スレッドが終わらない")

    def test_sending_when_not_connected_still_reports_an_error(self):
        """従来どおり、未接続の送信はその場でエラーを知らせること。"""
        port = _SlowPort(delay=0.0)
        conn = self._conn(port)
        conn._is_connected = False
        errors = []
        conn.error_occurred.connect(errors.append)
        conn.send_command("a")
        self.assertEqual(len(errors), 1)
        self.assertIn("接続されていません", errors[0])
        self.assertEqual(port.writes, [])


if __name__ == "__main__":
    unittest.main()
