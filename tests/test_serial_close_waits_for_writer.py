"""join が時間切れになったとき、書き込み中のポートを閉じないことを検証する。

dispose() は送信スレッドを join(timeout=2) で待つが、待ち切れなかった場合も
そのまま close へ進んでいた。pyserial の Win32 実装は close() で CancelIoEx
を投げた直後に、待機中の hEvent とポートのハンドルを閉じる。write の中で
GetOverlappedResult を待っている送信スレッドの足元からハンドルが消えるので、
Microsoft が CancelIoEx のドキュメントで明示的に禁じている順序になる。

2 秒を超える単発の write で起きる（9600 baud なら約 2.4KB 以上の貼り付け）。

閉じる役を送信スレッドへ渡し、write から戻ったスレッド自身が閉じることで、
ポートを開いたまま取り残さずに順序を守る。
"""
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")


class _BlockingPort:
    """write がテストの合図まで戻らない疑似ポート。呼ばれた順を記録する。"""

    def __init__(self):
        self.is_open = True
        self.in_waiting = 0
        self.writes = []
        self.events = []
        self.in_write = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()

    def write(self, data):
        self.events.append("write-start")
        self.in_write.set()
        self.release.wait(10)
        self.writes.append(bytes(data))
        self.events.append("write-end")
        return len(data)

    def flush(self):
        self.events.append("flush")

    def read(self, n):
        return b""

    def close(self):
        self.events.append("close")
        self.is_open = False
        self.closed.set()


class SerialCloseWaitsForWriterTest(unittest.TestCase):
    def _conn(self, port):
        from core.serial_connection import SerialConnection
        conn = SerialConnection("COM99", 9600)
        conn.serial_conn = port
        conn._is_connected = True
        conn._should_stop = False
        return conn

    def test_a_timed_out_join_does_not_close_the_port_under_the_writer(self):
        """join が空振りしたら、その場では閉じないこと。"""
        port = _BlockingPort()
        conn = self._conn(port)
        conn.send_command("a")
        self.assertTrue(port.in_write.wait(3), "送信が始まっていない")

        conn.dispose()      # join(timeout=2) が空振りする

        self.assertNotIn("close", port.events,
                         "書き込みの最中にポートを閉じた: %r" % (port.events,))

    def test_the_writer_closes_the_port_it_was_left_with(self):
        """空振りのあと、write から戻った送信スレッド自身がポートを閉じること。"""
        port = _BlockingPort()
        conn = self._conn(port)
        conn.send_command("a")
        self.assertTrue(port.in_write.wait(3), "送信が始まっていない")

        conn.dispose()      # join(timeout=2) が空振りする
        port.release.set()  # 送信スレッドが write から戻る

        self.assertTrue(port.closed.wait(5),
                        "取り残されたポートが閉じられない: %r" % (port.events,))
        self.assertLess(port.events.index("write-end"), port.events.index("close"),
                        "書き込みの最中にポートを閉じた: %r" % (port.events,))

    def test_a_writer_that_finished_in_time_leaves_the_closing_to_dispose(self):
        """join が間に合ったときは、従来どおり dispose が閉じて二重に閉じないこと。"""
        port = _BlockingPort()
        port.release.set()
        conn = self._conn(port)
        conn.send_command("a")
        self.assertTrue(port.in_write.wait(3), "送信が始まっていない")

        conn.dispose()

        self.assertEqual(port.events.count("close"), 1,
                         "閉じた回数が 1 ではない: %r" % (port.events,))
        self.assertLess(port.events.index("write-end"), port.events.index("close"),
                        "書き込みの最中にポートを閉じた: %r" % (port.events,))

    def test_the_writer_does_not_close_a_port_it_was_not_left_with(self):
        """役を渡されていない送信スレッドは、ポートを閉じないこと。"""
        port = _BlockingPort()
        port.release.set()
        conn = self._conn(port)
        conn.send_command("a")
        self.assertTrue(port.in_write.wait(3), "送信が始まっていない")

        # dispose を通さずに、送信スレッドだけを終わらせる
        writer = conn._write_thread
        conn._send_queue.put(None)
        writer.join(timeout=3)
        self.assertFalse(writer.is_alive(), "送信スレッドが終わらない")

        self.assertNotIn("close", port.events,
                         "役を渡していないのに閉じた: %r" % (port.events,))
        conn.dispose()

    def test_a_port_without_a_writer_is_still_closed(self):
        """一度も送信していない接続でも、後始末でポートを閉じること。"""
        port = _BlockingPort()
        conn = self._conn(port)

        conn.dispose()

        self.assertIn("close", port.events, "ポートが閉じられていない")


if __name__ == "__main__":
    unittest.main()
