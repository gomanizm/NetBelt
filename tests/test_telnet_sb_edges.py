"""サブネゴシエーションの終端検出の端を検証する。

終端 IAC SE を `find(b"\\xff\\xf0")` で探していた。2 つの穴がある。

1. 大きすぎる SB を捨てて読み飛ばし中（_discarding_sb）に、終端が
   TCP の切れ目でちょうど割れる（今回の受信が IAC で終わり、次の受信が
   SE で始まる）と、末尾の IAC を持ち越さずに捨てるため、次の受信では
   SE 単独しか見えず、二度と再同期しない。以後の受信は全部捨てられ、
   プロンプトも、DO への WONT 応答も消える。復旧は再接続だけ。

2. SB 本文の中のエスケープされた 0xFF（IAC IAC）を考慮していない。
   本文に `0xFF 0xFF 0xF0` が並ぶと 2 つ目の 0xFF と 0xF0 を IAC SE と
   誤認して SB を早く閉じ、残りの本文が通常データとして画面へ漏れる。
   本文中の `IAC DO x` まで本物の交渉として処理され、機器へ余計な
   WONT を返す。NAWS で幅 0xFFF0 のような値でも起こる。

どちらも前提は狭い（1 は 4096 バイト超の SB、2 は本文中の 0xFF 0xF0）が、
1 は当たるとセッションが黙って死ぬ。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

IAC, SB, SE, DO, WONT = 255, 250, 240, 253, 252


class TelnetSbEdgeTest(unittest.TestCase):
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

    def _oversized_sb(self, conn):
        """読み飛ばし状態に入る、終端の無い大きな SB。"""
        return bytes([IAC, SB, 24, 0]) + b"X" * (conn.MAX_PENDING_BYTES + 500)

    # --- 1. 読み飛ばし中に終端が割れる ---

    def test_a_terminator_split_between_iac_and_se_still_resynchronises(self):
        """IAC で終わり SE で始まる受信でも、その後の本文が出ること。"""
        conn, _ = self._conn()
        out, _ = self._feed(conn,
                            self._oversized_sb(conn),
                            b"more" + bytes([IAC]),
                            bytes([SE]) + b"Router>")
        self.assertEqual(out, b"Router>",
                         "終端が割れると再同期できず、以後を捨て続けている")

    def test_negotiation_after_a_split_terminator_is_still_answered(self):
        """再同期後の DO には、これまでどおり WONT を返すこと。"""
        conn, sent = self._conn()
        self._feed(conn,
                   self._oversized_sb(conn),
                   b"more" + bytes([IAC]),
                   bytes([SE]) + bytes([IAC, DO, 1]) + b"Router>")
        self.assertIn(bytes([IAC, WONT, 1]), sent,
                      "再同期できず、交渉に応答していない")

    # --- 2. 本文中のエスケープされた 0xFF ---

    def test_an_escaped_iac_inside_the_body_is_not_a_terminator(self):
        """本文の IAC IAC 0xF0 で SB を閉じず、残りを漏らさないこと。"""
        conn, _ = self._conn()
        body = b"A" + bytes([IAC, IAC, 0xF0]) + b"LEAK"
        out, _ = self._feed(conn,
                            bytes([IAC, SB, 24, 0]) + body + bytes([IAC, SE])
                            + b"Router>")
        self.assertEqual(out, b"Router>",
                         "本文の続きが画面へ漏れている: %r" % out)

    def test_a_negotiation_byte_pattern_inside_the_body_is_not_answered(self):
        """本文に IAC DO が現れても、交渉として扱わないこと。"""
        conn, sent = self._conn()
        body = b"A" + bytes([IAC, IAC, 0xF0]) + bytes([IAC, DO, 1])
        self._feed(conn,
                   bytes([IAC, SB, 24, 0]) + body + bytes([IAC, SE])
                   + b"Router>")
        self.assertEqual(sent, [],
                         "SB 本文を交渉として処理し、余計な応答を返している")

    def test_an_escaped_iac_in_the_body_of_a_discarded_sb_is_skipped(self):
        """読み飛ばし中も同じ規則で終端を探すこと。"""
        conn, _ = self._conn()
        out, _ = self._feed(conn,
                            self._oversized_sb(conn),
                            bytes([IAC, IAC, 0xF0]) + b"LEAK" + bytes([IAC, SE])
                            + b"Router>")
        self.assertEqual(out, b"Router>",
                         "読み飛ばし中に本文の続きが漏れている: %r" % out)

    def test_a_naws_width_of_fff0_does_not_leak(self):
        """NAWS の幅 0xFFF0（エスケープして FF FF F0）でも漏れないこと。"""
        conn, _ = self._conn()
        # 幅 0xFFF0 → FF FF F0、高さ 0x0018 → 00 18
        naws = bytes([IAC, SB, 31]) + bytes([IAC, IAC, 0xF0, 0x00, 0x18]) + bytes([IAC, SE])
        out, _ = self._feed(conn, naws + b"Router>")
        self.assertEqual(out, b"Router>",
                         "NAWS の値が画面へ漏れている: %r" % out)

    # --- 既存の振る舞いは保つ ---

    def test_a_normal_sb_is_still_swallowed(self):
        conn, sent = self._conn()
        out, pending = self._feed(conn,
                                  bytes([IAC, SB, 24, 0]) + b"VT100" + bytes([IAC, SE])
                                  + b"Router>")
        self.assertEqual(out, b"Router>")
        self.assertEqual(pending, b"")
        self.assertEqual(sent, [])

    def test_an_sb_whose_body_ends_in_a_lone_iac_is_carried_over(self):
        """本文が IAC で切れて次の受信に続く場合は、持ち越すこと。"""
        conn, _ = self._conn()
        out, _ = self._feed(conn,
                            bytes([IAC, SB, 24, 0]) + b"AB" + bytes([IAC]),
                            bytes([IAC]) + b"CD" + bytes([IAC, SE]) + b"Router>")
        self.assertEqual(out, b"Router>",
                         "本文途中で切れた IAC の扱いがずれている: %r" % out)


if __name__ == "__main__":
    unittest.main()
