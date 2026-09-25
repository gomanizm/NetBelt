"""Telnet で相手が RST で消えたあと、交渉の応答の失敗を 1 回だけ知らせることを検証する。

何が起きていたか（基準 441ea02 で実測、localhost）。受信スレッドは、1 回の
受信（最大 4,096 バイト＝ IAC DO/WILL が約 1,365 個）に入っていた交渉の
それぞれへ _send_telnet_command で応答を書く。相手が RST で閉じていると
書き込みは毎回 ConnectionResetError になり、そのたびに error_occurred
『Telnet交渉の応答を送信できませんでした: [WinError 10054] …』を出していた。
is_connected を落とさないので、次の応答もまた書こうとして失敗する。
  - 相手が IAC DO ECHO を 20,000 個送った直後に RST で閉じる: 5 回とも
    483〜602 回出た。
  - 書き込みだけが ConnectionResetError になるソケットへ差し替え、4,095
    バイトの DO を送らせる: 1,069 回。
  MainWindow は最初の 1 件で接続を外すが、残りはすべて「置き換え済みの接続
  からの通知」として 1 件ずつ print と dispose() と deleteLater を行っていた。

どう直したか。応答の書き込みに失敗したら、接続中のときだけ is_connected を
落としてから 1 回だけ知らせる。以後の応答は書かず（_send_telnet_command は
接続中のときしか書かない）、受信ループも次の周で抜ける。GUI は
error_occurred で後始末と再接続待ちに入るので、画面の表示は今と同じ。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")

IAC, DO, ECHO = 255, 253, 1


def _reset_error():
    return ConnectionResetError(
        10054, "既存の接続はリモート ホストに強制的に切断されました。")


class _ResetSocket:
    """送るたびに RST を受けたあとと同じ例外を投げるソケット（偽物）"""

    def send(self, data):
        raise _reset_error()

    def sendall(self, data):
        raise _reset_error()


class _ResetOnWrite:
    """受信は本物、送信だけ RST を受けたあとと同じ例外を返すソケットの包み"""

    def __init__(self, sock):
        self._sock = sock

    def send(self, data):
        raise _reset_error()

    def sendall(self, data):
        raise _reset_error()

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _Peer:
    """1 接続だけ受ける localhost の相手"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.peer = None
        self.accepted = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        try:
            self.peer, _ = self.sock.accept()
        except OSError:
            return
        self.accepted.set()

    def close(self):
        for s in (self.peer, self.sock):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass


class TelnetReplyFailureReportedOnceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()

    def test_many_replies_to_a_reset_peer_raise_one_error(self):
        """1 回の受信に入っていた 100 個の DO へ、知らせは 1 回だけ"""
        from core.telnet_connection import TelnetConnection
        conn = TelnetConnection("192.0.2.1", 23)
        conn.socket = _ResetSocket()
        conn.is_connected = True
        errors = []
        conn.error_occurred.connect(errors.append)

        conn._process_telnet_commands(bytes([IAC, DO, ECHO]) * 100)

        self.assertEqual(1, len(errors),
                         "応答の失敗が %d 回知らされた" % len(errors))
        self.assertIn("Telnet交渉の応答を送信できませんでした", errors[0])
        self.assertFalse(conn.is_connected,
                         "応答を書けなかったのに接続中のまま（次の応答も書こうとする）")

    def test_a_reset_seen_by_the_reader_thread_is_reported_once(self):
        """本物の受信スレッドでも、4,095 バイトの DO への知らせは 1 回だけ"""
        from core.telnet_connection import TelnetConnection
        peer = _Peer()
        self.addCleanup(peer.close)
        conn = TelnetConnection("127.0.0.1", peer.port)
        self.addCleanup(conn.dispose)
        errors = []
        closed = []
        conn.error_occurred.connect(errors.append)
        conn.disconnected.connect(lambda: closed.append(True))
        self.addCleanup(conn.disconnected.disconnect)
        self.assertTrue(conn.connect(), "前提: localhost の相手に繋がる")
        self.assertTrue(peer.accepted.wait(5))

        # RST が受信と応答の書き込みの間に着いた状態（書き込みだけが失敗する）
        conn.socket = _ResetOnWrite(conn.socket)
        peer.peer.sendall(bytes([IAC, DO, ECHO]) * 1365)
        reader = conn._read_thread
        self._pump(5, until=lambda: errors and not reader.is_alive())
        self._pump(0.3)

        self.assertEqual(1, len(errors),
                         "応答の失敗が %d 回知らされた" % len(errors))
        self.assertIn("Telnet交渉の応答を送信できませんでした", errors[0])
        self.assertFalse(conn.is_connected)
        self.assertFalse(reader.is_alive(), "受信スレッドが抜けていない")


if __name__ == "__main__":
    unittest.main()
