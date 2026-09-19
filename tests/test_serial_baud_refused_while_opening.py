"""ポートを開いている最中に変えたボーレートが拒まれたとき、実際の速度を名乗ることを検証する。

MainWindow は接続スレッドを起こす前に接続を登録するので、serial.Serial() が
ポートを開いている間（通常は数十 ms）にツリーからボーレートを変えられる。
このとき set_baudrate() はまだ開いていないポートの代わりに値だけを覚えて
True を返し、connect() は開き終えたポートへ _apply_baudrate() で合わせ直す。

実測で起きていたこと: 115200 を拒むポートで開いている最中に 115200 へ
変えると、_apply_baudrate() の False が無視され、ポートは 9600 のままなのに
conn.baudrate は 115200、端末には「接続しました: COM3 (115200 baud)」と
出ていた。画面の経路（ツリーの _set_baudrate）でも同じで、ステータスバーは
「ボーレートを 115200 baud に変更しました」になった。通信は文字化けしたまま。

直し方: 拒まれたら conn.baudrate を開いたときの値へ戻し、接続メッセージは
実際の速度で出し、変更できなかったことを端末へ 1 行出す。接続済みで
拒まれたときの方針（接続は保ち、そう知らせる）に合わせた。
"""
import os
import sys
import threading
import unittest
from unittest import mock

import serial

sys.path.insert(0, "src")


class _PickyPort:
    """pyserial と同じ順で値を書き換え、refuse に挙げた値だけを拒む疑似ポート。"""

    def __init__(self, baudrate=9600, refuse=()):
        self._baudrate = baudrate
        self.refuse = set(refuse)
        self.is_open = True
        self.in_waiting = 0

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value):
        self._baudrate = value
        if value in self.refuse:
            raise serial.SerialException("SetCommState failed")

    def read(self, n):
        return b""

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


class BaudRefusedWhileOpeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _connect_changing_baud_while_opening(self, new_baudrate, refuse):
        """ポートを開いている最中に new_baudrate へ変え、開き終えさせる。

        戻り値は (接続, set_baudrate の戻り値, connect の戻り値, 開いたポート,
        端末へ出た文言の並び)。
        """
        from PyQt6.QtCore import Qt
        from core.serial_connection import SerialConnection

        opening = threading.Event()
        proceed = threading.Event()
        self.addCleanup(proceed.set)
        created = []

        def slow_serial(**kwargs):
            opening.set()
            proceed.wait(5.0)
            port = _PickyPort(kwargs["baudrate"], refuse=refuse)
            created.append(port)
            return port

        conn = SerialConnection("COM3", 9600)
        self.addCleanup(conn.dispose)
        output = []
        conn.output_received.connect(
            output.append, type=Qt.ConnectionType.DirectConnection)
        outcome = {}
        with mock.patch("core.serial_connection.serial.Serial",
                        side_effect=slow_serial):
            worker = threading.Thread(
                target=lambda: outcome.setdefault("connected", conn.connect()),
                daemon=True)
            worker.start()
            self.assertTrue(opening.wait(5.0), "前提: ポートを開き始めていない")
            accepted = conn.set_baudrate(new_baudrate)
            proceed.set()
            worker.join(5.0)
        self.assertFalse(worker.is_alive(), "connect() が戻ってこない")
        self.assertEqual(len(created), 1, "前提: ポートが開いていない")
        return conn, accepted, outcome.get("connected"), created[0], output

    def test_a_refused_change_keeps_the_real_baudrate(self):
        """拒まれたら、接続は開いたときの速度を名乗ること。"""
        conn, accepted, connected, port, _ = \
            self._connect_changing_baud_while_opening(115200, refuse={115200})

        self.assertTrue(accepted, "前提: 開く前の変更は値だけ受け付ける")
        self.assertTrue(connected, "変更を拒まれただけで接続を諦めている")
        self.assertEqual(port.baudrate, 9600, "前提: ポートは 9600 のまま")
        self.assertEqual(conn.baudrate, 9600,
                         "ポートは 9600 のままなのに、接続が 115200 を名乗っている")

    def test_the_connect_message_names_the_real_baudrate_and_says_why(self):
        """接続メッセージが実際の速度を名乗り、変更できなかったことを知らせること。"""
        _, _, _, _, output = \
            self._connect_changing_baud_while_opening(115200, refuse={115200})
        text = "".join(output)

        self.assertIn("接続しました: COM3 (9600 baud)", text,
                      "接続メッセージが実際の速度を名乗っていない: %r" % text)
        self.assertNotIn("(115200 baud)", text)
        self.assertIn("115200 baud に変更できなかった", text,
                      "変更できなかったことを知らせていない: %r" % text)

    def test_an_accepted_change_is_still_applied(self):
        """対照: 受け付けられた変更は、これまでどおり開いたポートへ効くこと。"""
        conn, _, connected, port, output = \
            self._connect_changing_baud_while_opening(115200, refuse=())

        self.assertTrue(connected)
        self.assertEqual(port.baudrate, 115200)
        self.assertEqual(conn.baudrate, 115200)
        self.assertIn("接続しました: COM3 (115200 baud)", "".join(output))
        self.assertNotIn("変更できなかった", "".join(output))


if __name__ == "__main__":
    unittest.main()
