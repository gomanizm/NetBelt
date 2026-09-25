"""Telnet の送信の背圧の経路（見張りのある接続）を、テストが直接守っていなかった所。

後始末で見張りを止めること（tests-04）
  TelnetConnection.dispose は、送信が詰まって動いている見張り（DrainWatcher）を
  watcher.stop() で止める。止めないと、見張りは is_connected=False を見て
  「書ける」と判定し、send_drained を 1 回出してから終わる。既存のテスト
  （test_telnet_paste_to_slow_reader.py ほか）は、dispose のあとにスレッドが
  終わることとエラーが出ないことしか見ておらず、スレッドは stop が無くても
  終わるので気づけなかった。実測（441ea02）: dispose の watcher.stop() を消した版で、
  Telnet 関係の既存テスト 11 ファイル・39 件はすべて通った。
  ここでは localhost の読まない相手へ送って詰まらせ、見張りが動いている状態で
  dispose し、そのあとに send_drained が 1 回も出ないことを確かめる（stop を
  消した版では 1 回出て落ちる）。製品は変えていない。
  dispose は is_connected を落としてから stop を呼ぶので、その間に見張りが
  知らせる隙間は理屈の上では残る（441ea02 で 30 回流して 0 回）。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")


class _NonReadingPeer:
    """受け入れるだけで読まない相手（localhost）。受信バッファを絞る"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.peer = None
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        try:
            self.peer, _ = self.sock.accept()
        except OSError:
            pass

    def close(self):
        for s in (self.peer, self.sock):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])


class DisposeStopsTheDrainWatcherTest(_Base):

    def test_no_drain_notice_after_dispose(self):
        from PyQt6.QtCore import Qt
        from core.telnet_connection import TelnetConnection
        peer = _NonReadingPeer()
        self.addCleanup(peer.close)
        conn = TelnetConnection("127.0.0.1", peer.port)
        self.addCleanup(conn.dispose)
        self.assertTrue(conn.connect(), "前提: localhost に繋がる")
        # 詰まるかを OS の送信バッファの大きさに任せない（Windows は自動で大きくする）
        conn.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        notices = []
        conn.send_drained.connect(lambda: notices.append(1),
                                  Qt.ConnectionType.DirectConnection)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not conn.has_pending_sends():
            conn.send_command("x" * 4096)
        watcher = conn._drain_watcher
        self.assertIsNotNone(watcher, "前提: 見張りがある")
        thread = watcher._thread
        self.assertIsNotNone(thread, "前提: 送信が詰まり、見張りが動いている")
        del notices[:]

        conn.dispose()
        thread.join(1.5)
        time.sleep(0.2)

        self.assertFalse(thread.is_alive(), "見張りのスレッドが残った")
        self.assertEqual([], notices, "dispose のあとに send_drained が出た")


if __name__ == "__main__":
    unittest.main()
