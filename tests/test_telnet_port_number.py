"""Telnet でも、設定のポートを整数へそろえてから繋ぐことを検証する。

何が起きていたか（基準 028ebc2 で実測）。TelnetConnection.connect は設定の
ポートをそのまま socket.connect へ渡していた。手編集の config.json などで
"port": "23" と文字列になっていると、待ち受けているポートでも繋がらず、
「予期しないエラー: 'str' object cannot be interpreted as an integer」とだけ
出ていた（"abc"・""・null も同じ文面）。70000 は英語の
「connect(): port must be 0-65535.」、true は 1 番へ繋ぎに行っていた。
SSH は 4f472da で「ポート番号 … 1〜65535 の整数」と案内するように
なったので、同じ設定の誤りでも接続方式によって振る舞いが食い違っていた。

どう直したか。SSH と同じ読み方（tcp_port_number: 前後の空白を許す整数の
文字列と、端数の無い数を整数へ。bool・範囲外・読めない値は使わない）を
core/sockets.py に置き、TelnetConnection.connect の入口でソケットを作る前に
そろえる。使えない値なら、機器へは何も繋がずに日本語のエラーで止める。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")


class _Listener:
    """localhost のエフェメラルポートで受け付けた数だけを数える。"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepted = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted.append(conn)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for conn in self.accepted:
            conn.close()


class TelnetPortNumberTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.listener = _Listener()
        self.addCleanup(self.listener.close)

    def _connect(self, port):
        from core.telnet_connection import TelnetConnection
        conn = TelnetConnection("127.0.0.1", port)
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        returned = conn.connect()
        opened_socket = conn.socket
        conn.dispose()
        return conn, returned, errors, opened_socket

    def _wait_accepted(self, count):
        deadline = time.monotonic() + 3
        while len(self.listener.accepted) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return len(self.listener.accepted)

    def test_string_port_connects(self):
        """文字列の "ポート番号" でも、そのポートへ繋がること。"""
        for value in (str(self.listener.port), " %d " % self.listener.port):
            with self.subTest(port=value):
                before = len(self.listener.accepted)
                conn, returned, errors, _ = self._connect(value)

                self.assertTrue(returned, errors)
                self.assertEqual(errors, [])
                self.assertEqual(self._wait_accepted(before + 1), before + 1)
                self.assertEqual(conn.port, self.listener.port)
                self.assertIs(type(conn.port), int)

    def test_integer_port_still_connects(self):
        """整数のポートは今までどおり繋がること（対照）。"""
        conn, returned, errors, _ = self._connect(self.listener.port)

        self.assertTrue(returned, errors)
        self.assertEqual(conn.port, self.listener.port)

    def test_unusable_port_stops_before_connecting(self):
        """使えない値は、ソケットを作らずに日本語のエラーで止めること。"""
        for value in ("abc", "", None, 0, 70000, -1, True, 22.5, [23]):
            with self.subTest(port=value):
                conn, returned, errors, opened_socket = self._connect(value)

                self.assertFalse(returned)
                self.assertIsNone(opened_socket,
                                  "使えないポートのままソケットを作った")
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("ポート番号", errors[0])
                self.assertIn("1〜65535", errors[0])
        self.assertEqual(self.listener.accepted, [])


if __name__ == "__main__":
    unittest.main()
