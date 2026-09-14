"""Telnet 交渉の応答が、実際にソケットへ全部出ることを検証する。

_send_telnet_command は通常送信（send_command）と違って send を使い、
戻り値を捨てたうえ裸の except で例外を握り潰していた。

  1. send は送れたバイト数を返すだけで、渡した全部を送ったとは限らない。
     3バイトの IAC WONT/DONT が途中までしか出ないと、相手から見ると
     交渉としては成立せず、残り2バイトは次の送信にくっついて本文として
     届く。
  2. 裸の except は OSError だけでなく KeyboardInterrupt まで飲み込む。
     送信に失敗しても画面にも操作者にも何も出ないため、交渉が片方向で
     止まったまま原因が追えない。

既存のネゴシエーションのテストは _send_telnet_command 自体をリストの
append へ差し替えており、実送信の部分を一度も通っていなかった。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

IAC, DONT, WONT = 255, 254, 252
ECHO = 1


class _PartialSendSocket:
    """1回の send で1バイトしか受け取らないソケット。"""

    def __init__(self):
        self.wire = bytearray()

    def send(self, data):
        self.wire += data[:1]
        return 1

    def sendall(self, data):
        self.wire += data


class _RaisingSocket:
    """送信のたびに指定の例外を投げるソケット。"""

    def __init__(self, error):
        self.error = error

    def send(self, data):
        raise self.error

    def sendall(self, data):
        raise self.error


class TelnetNegotiationSendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _conn(self, sock):
        """機器へは繋がない。ソケットだけ偽物に差し替える。"""
        from core.telnet_connection import TelnetConnection
        conn = TelnetConnection("192.0.2.1", 23)
        conn.socket = sock
        conn.is_connected = True
        errors = []
        conn.error_occurred.connect(errors.append)
        return conn, errors

    def test_a_negotiation_reply_is_sent_in_full(self):
        sock = _PartialSendSocket()
        conn, _ = self._conn(sock)

        conn._send_telnet_command(bytes([IAC, WONT, ECHO]))

        self.assertEqual(bytes([IAC, WONT, ECHO]), bytes(sock.wire),
                         "交渉の応答が途中までしか出ていない: %r" % bytes(sock.wire))

    def test_two_replies_do_not_collapse_into_escaped_ff(self):
        sock = _PartialSendSocket()
        conn, _ = self._conn(sock)

        conn._send_telnet_command(bytes([IAC, WONT, ECHO]))
        conn._send_telnet_command(bytes([IAC, DONT, ECHO]))

        self.assertEqual(bytes([IAC, WONT, ECHO, IAC, DONT, ECHO]),
                         bytes(sock.wire),
                         "応答2件がそのまま並んでいない: %r" % bytes(sock.wire))

    def test_a_send_failure_is_reported(self):
        conn, errors = self._conn(_RaisingSocket(OSError("送信できません")))

        conn._send_telnet_command(bytes([IAC, WONT, ECHO]))

        self.assertTrue(errors, "送信失敗が握り潰されている")

    def test_a_keyboard_interrupt_is_not_swallowed(self):
        conn, _ = self._conn(_RaisingSocket(KeyboardInterrupt()))

        with self.assertRaises(KeyboardInterrupt):
            conn._send_telnet_command(bytes([IAC, WONT, ECHO]))


if __name__ == "__main__":
    unittest.main()
