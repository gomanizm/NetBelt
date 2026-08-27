"""Telnet のネゴシエーションが、TCP の切れ目をまたいでも壊れないことを検証する。

_process_telnet_commands は、渡されたバイト列の中でシーケンスが完結して
いる前提で書かれていた。途中で切れると、次の受信まで持ち越す仕組みが
無いため3通りに壊れる。

  1. IAC (0xFF) が末尾に来ると `i + 1 < len(data)` を外れ、通常データ
     として出力される。0xFF は UTF-8 デコードを必ず失敗させるので、
     以降の出力が閾値まで止まったうえ文字化けする。
  2. 3バイトの DO/WILL が割れると `i + 2 < len(data)` を外れ、オプション
     バイトを捨てたうえ WONT/DONT の応答も送らない。応答を待つ実装の
     機器では、ログインプロンプトが返ってこない。
  3. サブネゴシエーションの IAC SE が同じ受信に入らないと、残りを
     まるごと破棄する。

呼び出し元の _read_output も、UTF-8 デコードが成功すればバッファを
空にしてしまうので、持ち越す場所が無かった。

WAN 越し、MSS の小さい経路、機器が出力の合間に IAC を挟む場合に起きる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO = 1


class TelnetNegotiationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _conn(self):
        """機器へは繋がない。送信だけ横取りして中身を見る。"""
        from core.telnet_connection import TelnetConnection
        conn = TelnetConnection("192.0.2.1", 23)
        sent = []
        conn._send_telnet_command = sent.append
        return conn, sent

    def _feed(self, conn, *chunks):
        """受信を分割して流し込み、画面へ出る分をまとめて返す。"""
        out = bytearray()
        pending = b""
        for chunk in chunks:
            clean, pending = conn._process_telnet_commands(pending + chunk)
            out += clean
        return bytes(out), pending

    # --- 切れ目をまたいでも壊れないこと ---

    def test_an_iac_at_the_end_of_a_segment_does_not_leak(self):
        """IAC が受信の末尾に来ても、生バイトを画面へ出さないこと。"""
        out, _ = self._feed(self._conn()[0],
                            bytes([IAC]),
                            bytes([WILL, ECHO]) + b"Router>")
        self.assertEqual(out, b"Router>",
                         "ネゴシエーションの生バイトが画面へ漏れている")

    def test_a_three_byte_command_split_before_the_option_still_replies(self):
        """3バイトが割れても、オプションへの応答を送ること。

        応答を待つ機器では、返さないとプロンプトが出ないままになる。
        """
        conn, sent = self._conn()
        out, _ = self._feed(conn,
                            bytes([IAC, WILL]),
                            bytes([ECHO]) + b"Router>")
        self.assertEqual(out, b"Router>")
        self.assertIn(bytes([IAC, DONT, ECHO]), sent,
                      "割れた WILL に応答していない: %s" % sent)

    def test_a_do_split_before_the_option_still_replies(self):
        conn, sent = self._conn()
        out, _ = self._feed(conn,
                            bytes([IAC, DO]),
                            bytes([ECHO]) + b"Router>")
        self.assertEqual(out, b"Router>")
        self.assertIn(bytes([IAC, WONT, ECHO]), sent,
                      "割れた DO に応答していない: %s" % sent)

    def test_a_subnegotiation_split_across_segments_is_consumed(self):
        """サブネゴシエーションが割れても、残りを捨てないこと。"""
        out, _ = self._feed(self._conn()[0],
                            bytes([IAC, SB, 24, 0]),
                            b"VT100" + bytes([IAC, SE]) + b"Router>")
        self.assertEqual(out, b"Router>",
                         "サブネゴシエーションの分割で本文が失われている")

    def test_nothing_is_emitted_until_the_sequence_is_complete(self):
        """未完のシーケンスは、揃うまで画面へ出さないこと。"""
        conn, _ = self._conn()
        clean, pending = conn._process_telnet_commands(bytes([IAC, WILL]))
        self.assertEqual(clean, b"", "未完のシーケンスを画面へ出している")
        self.assertEqual(pending, bytes([IAC, WILL]),
                         "持ち越していない（次の受信で復元できない）")

    # --- 一度に届いたときの動きは変えないこと ---

    def test_a_whole_negotiation_is_still_handled(self):
        conn, sent = self._conn()
        out, pending = self._feed(
            conn, bytes([IAC, DO, ECHO]) + b"Router>")
        self.assertEqual(out, b"Router>")
        self.assertEqual(pending, b"")
        self.assertIn(bytes([IAC, WONT, ECHO]), sent)

    def test_an_escaped_ff_becomes_one_byte(self):
        """IAC IAC は 0xFF 1個として通すこと。"""
        out, _ = self._feed(self._conn()[0], bytes([IAC, IAC]) + b"x")
        self.assertEqual(out, bytes([IAC]) + b"x")

    def test_plain_data_passes_through_untouched(self):
        out, pending = self._feed(self._conn()[0], b"Router> show version\r\n")
        self.assertEqual(out, b"Router> show version\r\n")
        self.assertEqual(pending, b"")

    def test_a_peer_that_only_sends_iac_does_not_grow_the_buffer(self):
        """壊れた相手が IAC を送り続けても、溜め込み続けないこと。"""
        conn, _ = self._conn()
        pending = b""
        for _ in range(200):
            _, pending = conn._process_telnet_commands(
                pending + bytes([IAC, SB]) + b"x" * 100)
        self.assertLess(len(pending), 8192,
                        "未完のまま無制限に溜め込んでいる: %d バイト" % len(pending))


if __name__ == "__main__":
    unittest.main()
