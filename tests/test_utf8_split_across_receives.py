"""受信の切れ目で割れた UTF-8 が、画面とログで壊れないことを検証する。

SSH とシリアルは 1 回の受信ごとに errors='replace' で復号していた。
多バイト文字が受信境界をまたぐと、前半・後半がそれぞれ U+FFFD になり、
同じ文字列がセッションログにも渡るので原文は復元できない。シリアルは
in_waiting > 0 で即読むため、9600bps では 3 バイト文字の途中で読む
確率が高く、日本語を含む出力では頻発する。

Telnet は復号に失敗した分を溜めていたが、100 バイトを超えると強制的に
復号していた。先頭バイトだけが「�」になり、続きの継続バイトとプロンプトは
次に 100 バイトを超えるまで溜まったまま出ない。機器は入力が無ければ何も
送ってこないので、対話中はプロンプトが消えたように見える。

接続ごとにインクリメンタルデコーダを持てば、未完の末尾バイトは
デコーダの中に残り、次の受信で正しく結合される。
"""
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 「日」= E6 97 A5。1 バイト目と残りを別の受信に分ける
HEAD = b"abc\xe6"
TAIL = b"\x97\xa5def"
EXPECTED = "abc日def"


class SshSplitUtf8Test(unittest.TestCase):
    def _run(self, chunks):
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin")
        channel = mock.Mock()
        channel.closed = False
        pending = list(chunks)

        def recv_ready():
            if pending:
                return True
            channel.closed = True
            return False

        channel.recv_ready.side_effect = recv_ready
        channel.recv.side_effect = lambda n: pending.pop(0)
        conn.channel = channel
        conn.is_connected = True
        conn._stop_reading = False
        out = []
        conn.output_received.connect(out.append)
        conn._read_output()
        return out

    def test_a_character_split_across_two_receives_is_one_character(self):
        out = self._run([HEAD, TAIL])
        self.assertEqual("".join(out), EXPECTED,
                         "受信境界で割れた文字が化けている: %r" % out)
        self.assertNotIn("�", "".join(out))


class SerialSplitUtf8Test(unittest.TestCase):
    class _FakePort:
        """in_waiting とチャンク単位の read だけを持つ疑似ポート。

        データが尽きたら読み取りループを止める（本物なら dispose が止める）。
        """

        def __init__(self, conn, chunks):
            self._conn = conn
            self._chunks = list(chunks)
            self.is_open = True

        @property
        def in_waiting(self):
            if self._chunks:
                return len(self._chunks[0])
            self._conn._should_stop = True
            return 0

        def read(self, n):
            return self._chunks.pop(0)

        def close(self):
            self.is_open = False

    def _run(self, chunks):
        from core.serial_connection import SerialConnection
        conn = SerialConnection("COM99", 9600)
        conn.serial_conn = self._FakePort(conn, chunks)
        conn._is_connected = True
        conn._should_stop = False
        out = []
        conn.output_received.connect(out.append)
        conn._read_loop()
        return out

    def test_a_character_split_across_two_reads_is_one_character(self):
        out = self._run([HEAD, TAIL])
        self.assertEqual("".join(out), EXPECTED,
                         "受信境界で割れた文字が化けている: %r" % out)
        self.assertNotIn("�", "".join(out))


class TelnetSplitUtf8Test(unittest.TestCase):
    def _run(self, chunks):
        from core.telnet_connection import TelnetConnection
        conn = TelnetConnection("192.0.2.1", 23)
        sock = mock.Mock()
        pending = list(chunks)

        def recv(n):
            if pending:
                return pending.pop(0)
            # 機器はこちらが打たない限り何も送ってこない。切断ではなく
            # 「待っても来ない」状態で止めて、閉じるときの処理に頼らない
            conn._stop_reading = True
            raise socket.timeout()

        sock.recv.side_effect = recv
        conn.socket = sock
        conn.is_connected = True
        conn._stop_reading = False
        out = []
        conn.output_received.connect(out.append)
        conn._read_output()
        return out

    def test_a_character_split_across_two_receives_is_one_character(self):
        out = self._run([HEAD, TAIL])
        self.assertEqual("".join(out), EXPECTED)
        self.assertNotIn("�", "".join(out))

    def test_a_split_after_a_long_line_does_not_hold_back_the_prompt(self):
        """100 バイトを超えた後の分割で、続きのプロンプトが遅れないこと。"""
        head = b"a" * 101 + b"\xe6"
        tail = b"\x97\xa5Router>"
        out = self._run([head, tail])
        self.assertEqual("".join(out), "a" * 101 + "日Router>",
                         "プロンプトが出ていないか、化けている: %r" % out)


if __name__ == "__main__":
    unittest.main()
