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

途中までの送信と時間切れのあとの再開（tests-07）
  見張りのある接続は、送る列を _write_nowait で send の戻り値の分だけ進め、
  時間切れ（socket.timeout）なら切断にせず残りを列に持ったまま待ち、書ける
  ようになった知らせ（send_drained）で続きを書く。この分岐を直接通るテストが
  無かった。test_telnet_negotiation_send.py は見張りの無い接続を作るので
  sendall の経路しか通らず、遅い相手のテストは Windows のソケットでは途中までの
  send が起きないので踏まない。実測（441ea02）: _write_nowait に 3 種類の変異
  （送れたバイト数を見ずに渡した分を全部進める／時間切れで渡した分を捨てる／
  時間切れを切断扱いにする）を入れても、Telnet 関係の既存テスト 39 件は
  3 種類とも通った。壊れると、貼り付けたコンフィグの欠落やずれになる。
  ここでは send の戻り値と時間切れを台本どおりに返す偽のソケットを、見張りを
  持たせた接続に渡して確かめる。製品は変えていない。
"""
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

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


class _ScriptedSocket:
    """send の振る舞いを台本どおりに返す偽のソケット

    script の各要素: ("part", n) なら先頭 n バイトだけ受け取る、
    ("timeout",) なら時間切れ。台本が尽きたら渡された分を全部受け取る。
    """

    def __init__(self, script):
        self.script = list(script)
        self.wire = bytearray()

    def send(self, data):
        step = self.script.pop(0) if self.script else ("part", len(data))
        if step[0] == "timeout":
            raise socket.timeout("timed out")
        n = min(step[1], len(data))
        self.wire += data[:n]
        return n

    def sendall(self, data):
        raise AssertionError("見張りのある経路で sendall を使った")


class _BusySocket(_ScriptedSocket):
    """最初の 1 回は first バイトだけ受け取り、以後は busy の間だけ時間切れになる"""

    def __init__(self, busy, first):
        super().__init__([])
        self.busy = busy
        self.first = first

    def send(self, data):
        if self.first is not None:
            n, self.first = min(self.first, len(data)), None
            self.wire += data[:n]
            return n
        if self.busy["v"]:
            raise socket.timeout("timed out")
        self.wire += data
        return len(data)


class NowaitWriteTest(_Base):
    DATA = bytes(range(32, 127)) * 50        # 4,750 バイト

    def _conn(self, sock, busy):
        """機器へは繋がない。見張り（busy は差し替え可能）を持った接続の状態を作る"""
        import core.telnet_connection as tc
        from core.send_backpressure import DrainWatcher
        conn = tc.TelnetConnection("192.0.2.1", 23)
        conn.socket = sock
        conn.is_connected = True
        conn._drain_watcher = DrainWatcher(lambda: busy["v"], conn._announce_drained,
                                           wait=lambda: time.sleep(0.005))
        self.addCleanup(conn._drain_watcher.stop)
        errors = []
        conn.error_occurred.connect(errors.append)
        # 偽のソケットは select で調べられないので「書ける」と答えさせる
        patcher = mock.patch.object(tc, "socket_writable", lambda s: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return conn, errors

    def _pump(self, done, seconds=3):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not done():
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()

    def test_partial_sends_are_resumed_in_order(self):
        sock = _ScriptedSocket([("part", 7), ("part", 1), ("part", 1000), ("part", 3)])
        conn, errors = self._conn(sock, {"v": False})

        conn.send_command(self.DATA.decode("ascii"))

        self.assertEqual([], errors)
        self.assertEqual(self.DATA, bytes(sock.wire),
                         "途中までの送信の続きが欠けたか、ずれた")
        self.assertFalse(conn._out_data, "書き切ったのに列に残っている")

    def test_a_timeout_keeps_the_rest_and_resumes_when_drained(self):
        busy = {"v": True}
        sock = _BusySocket(busy, first=100)
        conn, errors = self._conn(sock, busy)

        conn.send_command(self.DATA.decode("ascii"))
        self.assertEqual([], errors, "時間切れが送信エラーになった")
        self.assertTrue(conn.is_connected, "時間切れで切断扱いになった")
        self.assertEqual(self.DATA[:100], bytes(sock.wire))
        self.assertTrue(conn.has_pending_sends(), "残りがあるのに次を渡させる")

        busy["v"] = False          # 書けるようになった → 見張りが send_drained を出す
        self._pump(lambda: len(sock.wire) >= len(self.DATA))

        self.assertEqual([], errors)
        self.assertEqual(self.DATA, bytes(sock.wire),
                         "時間切れのあとの続きが欠けたか、ずれた")


if __name__ == "__main__":
    unittest.main()
