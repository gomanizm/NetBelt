"""エスケープシーケンスの解釈を検証する。

実機(Ubuntu 24.04, term=vt100)で採取した実測に基づく:
  ESC[?2004h / ESC[?2004l  x3  ... readline のブラケットペースト。TERM に関係なく送られる
  ESC[0m / ESC[01;34m      x31 ... SGR(色)。解釈はしないが画面を汚してはいけない
OSC(ESC]) は vt100 では送られてこないが、送ってくる機器があると無限ループしていた。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

ESC = ""
BEL = ""


class TerminalEscapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _screen(self, payload):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        w.append_output("dev", payload)
        return w._terminals["dev"].toPlainText()

    # --- DECSET/DECRST (ESC[?<n>h / ESC[?<n>l) ---

    def test_bracketed_paste_on_is_not_printed(self):
        # 本件の不具合: 画面に "?2004h" がそのまま出ていた
        self.assertEqual(self._screen(ESC + "[?2004hPROMPT$ "), "PROMPT$ ")

    def test_bracketed_paste_off_is_not_printed(self):
        self.assertEqual(self._screen(ESC + "[?2004lDONE"), "DONE")

    def test_other_private_modes_are_not_printed(self):
        # 代替画面（?1049 / ?1047 / ?47）は読み飛ばさず、全画面アプリに
        # 入ったことを案内するようになったので、ここでは扱わない。
        # そちらは test_terminal_fullscreen.py が見る。
        for seq in ("[?25l", "[?25h", "[?1h"):
            with self.subTest(seq=seq):
                self.assertEqual(self._screen(ESC + seq + "X"), "X")

    # --- 既存の解釈を壊していないこと ---

    def test_sgr_is_still_consumed(self):
        self.assertEqual(self._screen(ESC + "[0mA" + ESC + "[01;34mB"), "AB")

    def test_sgr_with_colon_subparameters_does_not_crash(self):
        # ESC[38:2::255:0:0m 形式。パラメータに ':' を含む
        self.assertEqual(self._screen(ESC + "[38:2::255:0:0mA"), "A")

    # --- CSI 以外のエスケープ（無限ループしないこと） ---

    def test_osc_window_title_is_consumed(self):
        self.assertEqual(self._screen(ESC + "]0;cisco@ubuntu: ~" + BEL + "PROMPT$ "),
                         "PROMPT$ ")

    def test_osc_terminated_by_string_terminator_is_consumed(self):
        self.assertEqual(self._screen(ESC + "]0;title" + ESC + "\\" + "X"), "X")

    def test_two_byte_escapes_are_consumed(self):
        # ESC(B = ASCII 文字集合指定, ESC= / ESC> = キーパッドモード
        for seq in ("(B", "=", ">"):
            with self.subTest(seq=seq):
                self.assertEqual(self._screen(ESC + seq + "X"), "X")

    def test_lone_escape_at_end_does_not_hang(self):
        self.assertEqual(self._screen("X" + ESC), "X")


if __name__ == "__main__":
    unittest.main()
