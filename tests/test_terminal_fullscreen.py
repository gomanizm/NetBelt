"""全画面アプリと画面消去の扱いを、実機から採った生データで検証する。

`tests/fixtures/*.bin` は Ubuntu 24.04 へ SSH でつなぎ、NetBelt と同じ
`invoke_shell(term='vt100', width=80, height=24)` で採った受信バイトそのもの。
実環境を指す文字列だけ RFC 5737 のアドレスと架空のホスト名へ置き換えてある
（エスケープシーケンスは 1 バイトも変えていない）。

合成したシーケンスで検証したために一度取り違えた。端末種別を vt100 と
名乗っている以上、その terminfo に無いものは届かない:

  - nano は ESC[?1049h（代替画面）を **送らない**。届いていたのは
    ESC[1;24r（スクロール範囲）と ESC[1;79H（行・桁指定）だった。
  - clear は ESC[2J を **送らない**。ESC[H ESC[J が届く。

nano の画面が 1 行に潰れるのは、行・桁を指定して描く仕組みが無いため。
本対応は次の版。ここでは「なぜ崩れるのか」を伝えることだけを守る。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, "src")

ESC = "\x1b"
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def capture(name):
    """実機から採った受信バイトを、届いたときと同じ文字列で返す。"""
    raw = io.open(os.path.join(FIXTURES, name), "rb").read()
    return raw.decode("utf-8", "replace")


class TerminalFullScreenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    HISTORY = ("lab#show ip route\r\n"
               "  192.0.2.0/24 is directly connected, GigabitEthernet0/0\r\n")

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        return w

    def _screen(self, *payloads, history=False):
        w = self._widget()
        if history:
            w.append_output("dev", self.HISTORY)
        for payload in payloads:
            w.append_output("dev", payload)
        return w._terminals["dev"].toPlainText()

    def _notice(self):
        from ui.terminal_widget import TerminalWidget
        return TerminalWidget.ALT_SCREEN_NOTICE

    # --- 実機の nano ---------------------------------------------------

    def test_the_real_nano_capture_gets_a_notice(self):
        """実機の nano で案内が出ること。

        合成した ESC[?1049h では出ていたが、実機では 1 度も出なかった。
        vt100 を名乗っているため nano がそれを送らないのが理由。
        """
        text = self._screen(capture("nano_vt100.bin"), history=True)

        self.assertIn(self._notice(), text,
                      "実機の nano で案内が出ていない:\n" + text)

    def test_the_notice_comes_before_the_broken_paint(self):
        """崩れた画面より前に出ること。後ろだと混ざって読めない。"""
        text = self._screen(capture("nano_vt100.bin"), history=True)
        lines = text.split("\n")
        notice_at = next(i for i, l in enumerate(lines)
                         if self._notice() in l)
        garbled_at = next(i for i, l in enumerate(lines) if "Exit" in l)

        self.assertLess(notice_at, garbled_at,
                        "案内が崩れた画面の後ろに出ている:\n" + text)

    def test_the_notice_stands_on_its_own_line(self):
        """1 行として出ること。崩れた行に紛れ込ませない。"""
        text = self._screen(capture("nano_vt100.bin"), history=True)
        line = next(l for l in text.split("\n") if self._notice() in l)

        self.assertEqual(line.strip(), self._notice(),
                         "案内の行に他の出力が混ざっている: %r" % line)

    def test_nothing_of_the_app_is_drawn_before_the_notice(self):
        """アプリが 1 文字でも描く前に出ること。

        nano は目印を 2 つ送る。先に届くのはスクロール範囲の設定
        （ESC[1;24r）で、行・桁指定（ESC[1;79H）はタイトル行を
        描いたあと。後者だけを見ていると、案内が崩れた文字列の
        途中に割り込む。届く順を検証するのはここだけ。
        """
        text = self._screen(capture("nano_vt100.bin"), history=True)
        before = text.split(self._notice())[0]

        for marker in ("New Buffer", "Write Out", "Read File", "Exit"):
            self.assertNotIn(
                marker, before,
                "案内より前に nano が描き始めている（%r）:\n%s"
                % (marker, text))

    def test_the_history_before_it_survives(self):
        """nano を開いても、それまでの記録を消さないこと。"""
        text = self._screen(capture("nano_vt100.bin"), history=True)

        self.assertIn("show ip route", text)
        self.assertIn("directly connected", text)

    def test_the_notice_is_not_repeated(self):
        """開いている間、繰り返し出さないこと。

        代替画面を使っていないので抜けた合図も来ない。
        1 接続につき一度だけにしてある。
        """
        text = self._screen(capture("nano_vt100.bin"),
                            capture("nano_vt100.bin"), history=True)

        self.assertEqual(text.count(self._notice()), 1)

    def test_reconnecting_lets_it_be_shown_again(self):
        """再接続したら、また出せる状態に戻すこと。"""
        from ui.terminal_widget import TerminalWidget
        w = self._widget()
        w.append_output("dev", capture("nano_vt100.bin"))
        w.create_terminal_tab("dev")        # 同じ機器名で繋ぎ直す
        w.append_output("dev", capture("nano_vt100.bin"))

        self.assertEqual(
            w._terminals["dev"].toPlainText().count(
                TerminalWidget.ALT_SCREEN_NOTICE), 2,
            "再接続後に案内が出なくなっている")

    # --- 誤爆しないこと -------------------------------------------------

    def test_ordinary_shell_work_says_nothing(self):
        """普通のシェル操作で案内を出さないこと。

        実機で ls --color / ip / uname / rev を流したときの受信バイト。
        行・桁指定もスクロール範囲も 1 度も現れない。
        """
        text = self._screen(capture("shell_vt100.bin"), history=True)

        self.assertNotIn(self._notice(), text, "普通の操作で誤爆している")

    def test_clear_says_nothing(self):
        """clear でも案内を出さないこと。"""
        text = self._screen(capture("clear_vt100.bin"), history=True)

        self.assertNotIn(self._notice(), text)

    def test_private_modes_say_nothing(self):
        """ブラケットペーストやカーソル表示で誤爆しないこと。

        機器との普通のやりとりで頻繁に来る。
        """
        for seq in ("[?2004h", "[?25l", "[?25h", "[?1h", "[?7h"):
            with self.subTest(seq=seq):
                text = self._screen(ESC + seq + "X")
                self.assertNotIn(self._notice(), text)
                self.assertIn("X", text)

    def test_an_unparameterised_home_says_nothing(self):
        """引数なしの ESC[H / ESC[r は画面の初期化。全画面の合図ではない。"""
        for seq in ("[H", "[r"):
            with self.subTest(seq=seq):
                self.assertNotIn(self._notice(), self._screen(ESC + seq + "X"))

    def test_the_alternate_screen_still_counts(self):
        """代替画面を使う端末設定なら、そちらでも拾えること。"""
        for mode in ("1049", "1047", "47"):
            with self.subTest(mode=mode):
                self.assertIn(self._notice(),
                              self._screen(ESC + "[?" + mode + "h"))

    # --- 画面消去で記録を失わない ---------------------------------------

    def test_the_real_clear_keeps_the_session(self):
        """実機の clear で、それまでの出力を失わないこと。"""
        text = self._screen(capture("clear_vt100.bin"), history=True)

        self.assertIn("show ip route", text)
        self.assertIn("directly connected", text)

    def test_erase_whole_screen_does_not_delete_the_session(self):
        """ESC[2J が来ても記録ごと消さないこと（備え）。

        vt100 を名乗っている限り clear からは届かないが、そう送ってくる
        機器があったときに 30 分ぶんの出力を失うのは受け入れられない。
        """
        text = self._screen(ESC + "[2J", "prompt$ ", history=True)

        self.assertIn("show ip route", text, "画面消去で記録が消えた")

    def test_erase_scrollback_does_not_delete_it_either(self):
        text = self._screen(ESC + "[3J", "after", history=True)

        self.assertIn("show ip route", text)

    def test_erase_whole_screen_pushes_the_old_output_out_of_sight(self):
        """消さない代わりに、上へ送り出すこと。"""
        text = self._screen(ESC + "[2J", "prompt$ ", history=True)
        tail = text.split("directly connected, GigabitEthernet0/0")[1]

        self.assertGreaterEqual(tail.count("\n"), 10,
                                "画面から送り出せていない: %r" % text[-80:])

    # --- 既存の消去は壊さない -------------------------------------------

    def test_erase_to_end_of_screen_still_erases(self):
        """ESC[J（カーソルから末尾まで）は今までどおり消すこと。

        NX-OS が行編集で使う。
        """
        w = self._widget()
        w.append_output("dev", "abcdef")
        w.append_output("dev", "\b\b\b" + ESC + "[J")
        self.assertEqual(w._terminals["dev"].toPlainText(), "abc")

    def test_erase_to_start_of_line_still_erases(self):
        w = self._widget()
        w.append_output("dev", "abcdef")
        w.append_output("dev", "\b\b\b" + ESC + "[1J")
        self.assertEqual(w._terminals["dev"].toPlainText(), "def")


class FixtureIntegrityTest(unittest.TestCase):
    """採取データが、検証の材料として妥当であることを確かめる。"""

    def test_nano_never_asks_for_the_alternate_screen(self):
        """実機の nano が代替画面を使っていないこと。

        ここが崩れると、この検証全体の前提が変わる。
        """
        text = capture("nano_vt100.bin")
        for seq in ("[?1049h", "[?1047h", "[?47h"):
            self.assertNotIn(ESC + seq, text)

    def test_nano_does_set_a_scroll_region(self):
        """検知の目印が実際に含まれていること。"""
        import re
        self.assertTrue(re.search(r"\x1b\[\d+;\d+r", capture("nano_vt100.bin")))

    def test_ordinary_work_has_neither_marker(self):
        import re
        for name in ("shell_vt100.bin", "clear_vt100.bin"):
            with self.subTest(capture=name):
                text = capture(name)
                self.assertIsNone(re.search(r"\x1b\[\d+;\d+r", text))
                self.assertIsNone(re.search(r"\x1b\[\d+;\d+H", text))

    def test_the_captures_name_no_real_environment(self):
        """公開できる状態であること。"""
        import re
        for name in ("nano_vt100.bin", "shell_vt100.bin", "clear_vt100.bin"):
            with self.subTest(capture=name):
                text = capture(name)
                for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
                    self.assertTrue(
                        ip.startswith("192.0.2.") or ip.startswith("127."),
                        "実環境のアドレスが残っている: %s" % ip)


if __name__ == "__main__":
    unittest.main()
