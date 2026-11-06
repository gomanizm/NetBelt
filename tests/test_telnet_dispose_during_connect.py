# -*- coding: utf-8 -*-
"""TCP の接続待ちの最中に dispose() された Telnet 接続が、成立しないことを検証する。

何が起きていたか。connect() は socket.create_connection で繋ぐ。これは
繋がるまで self.socket へ代入しないので、接続待ち（最大 10 秒）の間に
タブを閉じて dispose() が走っても閉じる相手が無かった。あとから繋がると
is_connected=True・connected の通知・読み取りスレッドの起動まで進み、誰も
参照しない TCP セッション（機器の vty 枠）が残っていた。

接続待ちの最中に dispose() が走った状況は、create_connection を包んで
繋がった直後（戻る前）に dispose() を呼ぶことで、時間に頼らず作る。
ループバック（127.0.0.1）だけで確かめる。
"""
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from core import telnet_connection  # noqa: E402
from core.telnet_connection import TelnetConnection  # noqa: E402


class DisposeDuringConnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.server.settimeout(5)
        self.addCleanup(self.server.close)

    def _accept_peer(self):
        peer, _ = self.server.accept()
        self.addCleanup(peer.close)
        peer.settimeout(3)
        return peer

    def test_dispose_while_connecting_abandons_the_connection(self):
        conn = TelnetConnection("127.0.0.1", self.server.getsockname()[1])
        signals = []
        conn.connected.connect(lambda: signals.append("connected"))
        real_create = socket.create_connection

        def create_then_dispose(*args, **kwargs):
            sock = real_create(*args, **kwargs)
            # 接続待ちの間にタブが閉じられた
            conn.dispose()
            return sock

        with mock.patch.object(telnet_connection.socket, "create_connection",
                               side_effect=create_then_dispose):
            result = conn.connect()

        self.assertFalse(result)
        self.assertFalse(conn.is_connected)
        self.assertIsNone(conn.socket)
        self.assertTrue(conn._read_thread is None
                        or not conn._read_thread.is_alive())
        self.app.processEvents()
        self.assertEqual(signals, [])
        # 機器側から見て、セッションが閉じられている
        peer = self._accept_peer()
        self.assertEqual(peer.recv(1), b"")

    def test_connect_without_dispose_still_succeeds(self):
        conn = TelnetConnection("127.0.0.1", self.server.getsockname()[1])
        self.addCleanup(conn.dispose)
        self.assertTrue(conn.connect())
        self.assertTrue(conn.is_connected)
        self.assertIsNotNone(conn.socket)


if __name__ == "__main__":
    unittest.main()
