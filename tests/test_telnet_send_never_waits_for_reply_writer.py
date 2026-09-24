"""Telnet の送信で、GUI スレッドが受信スレッドの書き込みを待って固まらないことを検証する。

何が起きていたか（基準 87514a3 で実測、localhost）。送信の背圧の判定
（has_pending_sends → _send_backlogged）は書き込みの錠の locked() を読むだけで、
send_command は別に錠を取っていた（with self._write_lock）。判定が False に
なったあと、GUI スレッドが錠を取るまでの間に受信スレッドが交渉の応答を
書き始め、送信バッファが満杯で書けないと、受信スレッドは錠を持ったまま
0.1 秒の時間切れを繰り返して送り直し続ける。GUI スレッドは send_command の
錠の取得から戻れない。
  - 相手が読まないまま IAC DO を 30,000 個送り（応答は 90KB）、NetBelt の応答で
    送信バッファが埋まって受信スレッドが錠を持ったまま詰まってから
    send_command を呼ぶと、相手が 3 秒後に読み始めるまで戻らなかった
    （3.01 秒）。救済が無ければ永久に戻らない。GUI 全体（全タブ）が固まり、
    切断の操作もできない。

どう直したか。GUI スレッドは書き込みの錠を待たずに取る。送る分は接続の
送る列（持ち越し）へ積み、錠が取れてソケットへ待たずに書ける分だけ書く。
書き残しがあれば has_pending_sends を True にして見張りを始め、書けるように
なった知らせ（send_drained）で接続自身が GUI スレッドで続きを書く
（SSH の _carry と同じ考え方）。錠が取れないときは、いま書いている側が
続けて書くか、見張りが知らせる。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")

IAC, DO, WONT = 255, 253, 252
ECHO = 1


class NonReadingPeer:
    """読まないまま送り続けられる相手（localhost）。

    send_all は、送れた分だけ進める send で本当に送り終えるまで待つ
    （Windows は大きな sendall をカーネルへ丸ごと受け取って即座に返すため）。
    start_reading を呼ぶまで読まない。
    """

    def __init__(self):
        self.received = bytearray()
        self.stop = threading.Event()
        self.reading = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.peer = None

    def accept(self):
        self.sock.settimeout(5)
        self.peer, _ = self.sock.accept()
        self.peer.settimeout(0.2)
        threading.Thread(target=self._read, daemon=True).start()

    def send_all(self, data):
        view = memoryview(data)
        while view and not self.stop.is_set():
            try:
                view = view[self.peer.send(view[:4096]):]
            except socket.timeout:
                continue
            except OSError:
                return

    def start_reading(self):
        self.reading.set()

    def _read(self):
        while not self.stop.is_set():
            if not self.reading.wait(0.05):
                continue
            try:
                data = self.peer.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self.received.extend(data)

    def close(self):
        self.stop.set()
        self.reading.set()
        for sock in (self.peer, self.sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


class TelnetSendNeverWaitsForReplyWriterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump_until(self, done, seconds):
        end = time.time() + seconds
        while time.time() < end and not done():
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def test_send_returns_while_the_reader_cannot_write_its_replies(self):
        from core.send_backpressure import socket_writable
        from core.telnet_connection import TelnetConnection
        peer = NonReadingPeer()
        self.addCleanup(peer.close)
        conn = TelnetConnection("127.0.0.1", peer.port)
        self.addCleanup(conn.dispose)
        errors, closed = [], []
        conn.error_occurred.connect(errors.append)
        conn.disconnected.connect(lambda: closed.append(True))
        self.addCleanup(conn.disconnected.disconnect)
        self.assertTrue(conn.connect(), "前提: localhost の TCP に繋がる")
        peer.accept()

        # GUI の背圧の判定。まだ何も書いていないので、待たずに書けると答える
        self.assertFalse(conn.has_pending_sends(), "前提: 何も積んでいない")

        # ---- GUI はここ（判定のあと、send_command の前）で止まっていた ----
        # その間に相手が読まないまま交渉を連発し、NetBelt の応答が送信
        # バッファを埋める（応答 90KB > 送信バッファと相手の受信バッファ）
        count = 30000
        threading.Thread(
            target=peer.send_all, args=(bytes([IAC, DO, ECHO]) * count,),
            daemon=True).start()
        full_since = None
        deadline = time.time() + 15
        while time.time() < deadline:
            if socket_writable(conn.socket):
                full_since = None
            elif full_since is None:
                full_since = time.time()
            elif time.time() - full_since > 0.3:
                break
            time.sleep(0.01)
        self.assertIsNotNone(full_since, "前提: 応答で送信バッファが埋まらない")

        # 相手は 3 秒後に読み始める（救済。直す前はこれが無いと戻らない）
        rescue = threading.Timer(3.0, peer.start_reading)
        rescue.start()
        self.addCleanup(rescue.cancel)

        started = time.perf_counter()
        conn.send_command("show clock\r")        # GUI が再開する
        elapsed = time.perf_counter() - started

        self.assertLess(
            elapsed, 1.0,
            "GUI スレッドが send_command で %.2f 秒止まった"
            "（受信スレッドの書き込みを待った）" % elapsed)
        self.assertEqual([], errors)
        self.assertTrue(conn.is_connected)

        # 相手が読み始めたら、応答と打鍵が欠けず・混ざらずに届く
        peer.start_reading()
        typed = b"show clock\r"
        expected_len = 3 * count + len(typed)
        self._pump_until(
            lambda: len(peer.received) >= expected_len or errors or closed, 30)
        self.assertEqual([], errors)
        self.assertEqual([], closed)
        wire = bytes(peer.received)
        self.assertEqual(expected_len, len(wire), "届いたバイト数が違う")
        at = wire.find(typed)
        self.assertGreaterEqual(at, 0, "打鍵が届いていない")
        self.assertEqual(0, at % 3, "打鍵が交渉の応答の途中へ割り込んだ")
        self.assertEqual(bytes([IAC, WONT, ECHO]) * count,
                         wire[:at] + wire[at + len(typed):],
                         "交渉の応答が欠けたか、壊れた")


if __name__ == "__main__":
    unittest.main()
