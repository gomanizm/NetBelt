"""全画面アプリと画面消去の扱いを検証する。

実機(Ubuntu 24.04)で nano を開いたときに起きたこと:

  cisco@lab:~$ nano ~/.ssh/authorized_keys
  [ Reading... ][ Read 0 lines ]  GNU nano 7.2   /home/cisco/.ssh/^G cisco@lab:~$  ^\\ Replace ...

24 行の画面が 1 行に潰れている。このターミナルは行・桁を指定して描く
仕組み（ESC[H など）を持たず、すべてが今いる場所に書かれるため。
本対応は v1.2.0。ここでは 2 つだけ守る。

  1. 何が起きているか伝える。黙って崩れた画面を見せない。
  2. ESC[2J でセッションの記録を消さない。Linux で clear を打つだけで
     それまでの show 出力が全部消えるのは、機器を触る道具として困る。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

ESC = "\x1b"


class TerminalFullScreenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        return w

    def _screen(self, *payloads):
        w = self._widget()
        for payload in payloads:
            w.append_output("dev", payload)
        return w._terminals["dev"].toPlainText()

    # --- 全画面アプリに入ったことを伝える ---------------------------

    def test_entering_a_full_screen_app_says_so(self):
        """nano に入ったら、対応していないことを画面に出すこと。"""
        from ui.terminal_widget import TerminalWidget
        text = self._screen(ESC + "[?1049h")
        self.assertIn(TerminalWidget.ALT_SCREEN_NOTICE, text,
                      "案内が出ていない: %r" % text)

    def test_the_older_alternate_screen_modes_count_too(self):
        """1047 / 47 を使う実装もある。"""
        from ui.terminal_widget import TerminalWidget
        for mode in ("1047", "47"):
            with self.subTest(mode=mode):
                text = self._screen(ESC + "[?" + mode + "h")
                self.assertIn(TerminalWidget.ALT_SCREEN_NOTICE, text)

    def test_the_notice_is_not_repeated_while_inside(self):
        """入っている間、何度も出さないこと。画面が案内で埋まる。"""
        from ui.terminal_widget import TerminalWidget
        text = self._screen(ESC + "[?1049h", ESC + "[?1049h", ESC + "[?1049h")
        self.assertEqual(text.count(TerminalWidget.ALT_SCREEN_NOTICE), 1,
                         "案内が繰り返し出ている")

    def test_leaving_and_entering_again_says_so_again(self):
        """抜けてから入り直したら、もう一度伝えること。"""
        from ui.terminal_widget import TerminalWidget
        text = self._screen(ESC + "[?1049h", ESC + "[?1049l", ESC + "[?1049h")
        self.assertEqual(text.count(TerminalWidget.ALT_SCREEN_NOTICE), 2)

    def test_an_ordinary_private_mode_says_nothing(self):
        """ブラケットペーストやカーソル表示で案内を出さないこと。

        これらは機器との普通のやりとりで頻繁に来る。誤検知すると
        画面が案内だらけになる。
        """
        from ui.terminal_widget import TerminalWidget
        for seq in ("[?2004h", "[?25l", "[?25h", "[?1h", "[?7h"):
            with self.subTest(seq=seq):
                text = self._screen(ESC + seq + "X")
                self.assertNotIn(TerminalWidget.ALT_SCREEN_NOTICE, text)
                self.assertIn("X", text)

    def test_the_notice_survives_the_clear_that_follows(self):
        """案内の直後に nano が画面を消しても、案内が残ること。

        全画面アプリは入った直後に ESC[2J を送る。そこで記録を
        送り出すと、出したばかりの案内まで流れて読めなくなる。
        """
        from ui.terminal_widget import TerminalWidget
        text = self._screen(ESC + "[?1049h" + ESC + "[2J" + "GNU nano 7.2")
        self.assertIn(TerminalWidget.ALT_SCREEN_NOTICE, text)
        # 空行を数える。除いてしまうと、空行で押し出される不具合が
        # そのまま素通りする（実際に一度そうなった）。
        after = text.split(TerminalWidget.ALT_SCREEN_NOTICE, 1)[1]
        self.assertLess(after.count("\n"), 3,
                        "案内と本文の間に空行が入り、案内が画面外へ流れる:\n%r"
                        % text)

    def test_reconnecting_forgets_that_we_were_inside(self):
        """全画面アプリを開いたまま切断されたあと、再接続で元に戻ること。

        抜けるときの ESC[?1049l が来ないまま切れると、そのタブに
        「代替画面の中」が残る。タブは機器名で使い回されるので、
        再接続してもその状態が続き、このタブだけ clear が効かず、
        次に nano を開いても案内が出なくなる。
        """
        from ui.terminal_widget import TerminalWidget
        w = self._widget()

        # nano を開いたところで切断された
        w.append_output("dev", ESC + "[?1049h")
        self.assertTrue(w._terminals["dev"]._alt_screen)

        # 同じ機器名で再接続する（タブは使い回される）
        w.create_terminal_tab("dev")

        self.assertFalse(
            getattr(w._terminals["dev"], "_alt_screen", False),
            "切断前の代替画面の状態が残っている")

        # clear が効くこと
        w.append_output("dev", "output\r\n")
        before = w._terminals["dev"].toPlainText()
        w.append_output("dev", ESC + "[2J")
        after = w._terminals["dev"].toPlainText()
        self.assertGreater(after.count("\n"), before.count("\n") + 5,
                           "clear が効かないままになっている")

        # 案内がまた出ること
        w.append_output("dev", ESC + "[?1049h")
        self.assertIn(TerminalWidget.ALT_SCREEN_NOTICE,
                      w._terminals["dev"].toPlainText(),
                      "再接続後に案内が出なくなっている")

    # --- 画面消去で記録を失わない -----------------------------------

    def test_clear_does_not_throw_away_the_session(self):
        """clear（ESC[2J）で、それまでの出力を消さないこと。

        以前は文書全体を削除していたので、30 分ぶんの show 出力が
        clear ひとつで消えた。あとから貼り付けるための記録が失われる。
        """
        text = self._screen(
            "lab#show ip route\r\n",
            "  10.0.0.0/8 is directly connected, GigabitEthernet0/0\r\n",
            ESC + "[H" + ESC + "[2J" + ESC + "[3J",   # Linux の clear
            "lab#\r\n")

        self.assertIn("show ip route", text, "clear で記録が消えた")
        self.assertIn("directly connected", text, "clear で記録が消えた")

    def test_clearing_the_scrollback_does_not_throw_it_away_either(self):
        """ESC[3J 単体でも記録を消さないこと。"""
        text = self._screen("keep this\r\n", ESC + "[3J", "after")
        self.assertIn("keep this", text)

    def test_clear_still_pushes_the_old_output_out_of_sight(self):
        """消さない代わりに、上へ送り出すこと。

        何も起きないと、clear を打った利用者は効いていないと思う。
        """
        text = self._screen("old output\r\n", ESC + "[2J", "prompt$ ")
        blank_tail = text.split("old output")[1]
        self.assertGreaterEqual(blank_tail.count("\n"), 10,
                                "画面から送り出せていない: %r" % text[-80:])

    # --- 既存の消去は壊さない ---------------------------------------

    def test_erase_to_end_of_screen_still_erases(self):
        """ESC[J（カーソルから末尾まで）は今までどおり消すこと。

        NX-OS が行編集で使う。ここまで消さなくすると、打ち直した
        コマンドの残りが画面に残る。
        """
        w = self._widget()
        w.append_output("dev", "abcdef")
        w.append_output("dev", "\b\b\b" + ESC + "[J")
        self.assertEqual(w._terminals["dev"].toPlainText(), "abc")

    def test_erase_to_start_of_line_still_erases(self):
        """ESC[1J（先頭からカーソルまで）も今までどおり。"""
        w = self._widget()
        w.append_output("dev", "abcdef")
        w.append_output("dev", "\b\b\b" + ESC + "[1J")
        self.assertEqual(w._terminals["dev"].toPlainText(), "def")


if __name__ == "__main__":
    unittest.main()
