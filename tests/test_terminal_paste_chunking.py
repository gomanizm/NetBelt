"""長い貼り付けの間も、画面が固まらないことを検証する。

send_text は貼り付け・ドロップされた文字列を「for char in text」で1文字
ずつ key_pressed.emit していた。受け口は main_window で ssh/serial/telnet
の send_command へ直結しており、各 Connection は親が MainWindow なので
同一スレッド＝DirectConnection、つまり同期送信になる。

結果、貼り付け1回につき文字数ぶんの同期送信が GUI スレッドで回る。実測
（QT_QPA_PLATFORM=offscreen）では、2,000文字の送信中に 10ms 間隔の
QTimer の tick が 0 回だった（本来なら約1,000回）。再描画も操作も一切
通らず、中断する手立ても無い。コンソールへ設定を貼り付けるのは、この
ツールの中心的な使い方である。

送信そのものにかかる時間（9600bps なら 1文字≒1.04ms）は非同期にしても
縮まらない。縮められるのは「その間 GUI が息をするかどうか」で、
まとめて送りつつ、区切りごとにイベントループへ譲れば済む。

まとめて送るようにすると、これまで隠れていた部分送信が現実の問題になる。
socket.send も paramiko の Channel.send も、渡した全部を送ったとは限らず
送れたバイト数を返す。1文字ずつ送っている間はまず起きなかっただけで、
まとめた瞬間に取りこぼす。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class PasteChunkingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        terminal = w._terminals["dev"]
        terminal.set_input_enabled(True)
        sent = []
        terminal.key_pressed.connect(sent.append)
        return terminal, sent

    def _settle(self, rounds=200):
        """溜まった送信を最後まで流す。"""
        from PyQt6.QtWidgets import QApplication
        for _ in range(rounds):
            QApplication.instance().processEvents()

    def test_a_long_paste_is_not_sent_one_character_at_a_time(self):
        """1文字ずつ送らないこと。"""
        terminal, sent = self._terminal()
        body = "hostname R1\r" * 200          # 2,400 文字
        terminal.send_text(body)
        self._settle()

        self.assertLess(len(sent), 100,
                        "%d 文字を %d 回に分けて送っている" % (len(body), len(sent)))

    def test_the_paste_returns_before_everything_has_gone_out(self):
        """送り切るまで呼び出し元を止めないこと。

        止めている間はイベントループが回らず、再描画も操作も通らない。
        """
        terminal, sent = self._terminal()
        body = "x" * 5000
        terminal.send_text(body)

        self.assertLess(len("".join(sent)), len(body),
                        "送り切るまで戻ってきていない（その間ずっと固まる）")

    def test_the_whole_paste_arrives_in_order(self):
        """分けて送っても、中身と順序は変わらないこと。"""
        terminal, sent = self._terminal()
        body = "".join("line%03d\r" % i for i in range(300))
        terminal.send_text(body)
        self._settle()

        self.assertEqual("".join(sent), body,
                         "分割で中身か順序が変わっている")

    def test_a_second_paste_does_not_get_mixed_into_the_first(self):
        """続けて貼り付けても、混ざらないこと。"""
        terminal, sent = self._terminal()
        first = "A" * 3000
        second = "B" * 3000
        terminal.send_text(first)
        terminal.send_text(second)
        self._settle()

        self.assertEqual("".join(sent), first + second,
                         "2回の貼り付けが混ざっている")

    def test_a_short_paste_still_goes_out_at_once(self):
        """短い貼り付けはその場で送り切ること。"""
        terminal, sent = self._terminal()
        terminal.send_text("show version\r")
        self.assertEqual("".join(sent), "show version\r",
                         "短い貼り付けが遅れて出ている")

    def test_newlines_are_still_sent_as_carriage_returns(self):
        """改行の扱いは変えないこと。"""
        terminal, sent = self._terminal()
        terminal.send_text("conf t\n")
        self._settle()
        self.assertEqual("".join(sent), "conf t\r")


class PartialSendTest(unittest.TestCase):
    """まとめて送ると、部分送信を取りこぼす。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_ssh_sends_everything_it_was_given(self):
        """paramiko の Channel.send は送れた分しか送らない。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.channel = mock.Mock()
        conn.is_connected = True

        conn.send_command("A" * 4096)

        conn.channel.sendall.assert_called_once()
        conn.channel.send.assert_not_called()

    def test_telnet_sends_everything_it_was_given(self):
        """socket.send も渡した全部を送ったとは限らない。"""
        from core.telnet_connection import TelnetConnection
        conn = TelnetConnection("192.0.2.1", 23)
        conn.socket = mock.Mock()
        conn.is_connected = True

        conn.send_command("A" * 4096)

        conn.socket.sendall.assert_called_once()
        conn.socket.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
