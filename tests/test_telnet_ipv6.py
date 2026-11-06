# -*- coding: utf-8 -*-
"""Telnet でも IPv6 の接続先へ繋がることを検証する。

何が起きていたか。TelnetConnection.connect は AF_INET 固定のソケットを
作ってから connect していた。AF_INET のソケットは名前解決も IPv4 に限る
ので、"::1" や "2001:db8::1" のような IPv6 の接続先は、TCP を張る前に
「接続エラー: [Errno 11001] getaddrinfo failed」で断られていた。SSH は
同じ機器設定で繋がるので、接続方式によって振る舞いが食い違っていた。

ループバック（::1）だけで確かめる。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")


def _ipv6_loopback_available():
    if not socket.has_ipv6:
        return False
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.bind(("::1", 0))
    except OSError:
        return False
    finally:
        probe.close()
    return True


class _Listener6:
    """::1 のエフェメラルポートで受け付けた数だけを数える。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self.sock.bind(("::1", 0))
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


@unittest.skipUnless(_ipv6_loopback_available(), "IPv6 loopback unavailable")
class TelnetIPv6Test(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_connects_to_ipv6_literal(self):
        """IPv6 のアドレスを接続先にしても TCP が張れること。"""
        from core.telnet_connection import TelnetConnection
        listener = _Listener6()
        self.addCleanup(listener.close)

        conn = TelnetConnection("::1", listener.port)
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        returned = conn.connect()
        opened = conn.socket
        try:
            deadline = time.monotonic() + 3
            while not listener.accepted and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(returned, errors)
            self.assertEqual(errors, [])
            self.assertEqual(len(listener.accepted), 1)
            self.assertEqual(opened.family, socket.AF_INET6)
            # 読み取り用の短いタイムアウトは今までどおり
            self.assertEqual(opened.gettimeout(), 0.1)
        finally:
            conn.dispose()

    def test_ipv4_still_connects(self):
        """IPv4 の接続先は今までどおり繋がること。"""
        from core.telnet_connection import TelnetConnection
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        conn = TelnetConnection("127.0.0.1", server.getsockname()[1])
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        try:
            self.assertTrue(conn.connect(), errors)
            self.assertEqual(conn.socket.family, socket.AF_INET)
        finally:
            conn.dispose()


if __name__ == "__main__":
    unittest.main()
