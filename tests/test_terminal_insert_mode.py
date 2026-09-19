r"""挿入モード (IRM: ESC[4h / ESC[4l) で、印字が右の文字を送ることを検証する。

画面は通常モードの h/l (SM/RM) を処理しておらず、パーサが出した
Csi(params=(4,), final='h') を黙って捨て、印字は常に上書きしていた。
IRM を使う相手では、編集している内容と表示が食い違う。

実測: Screen(2, 10) に 'abcdef' ESC[3G ESC[4h 'X' ESC[4l を流すと、
画面は 'abXdef' (期待は 'abXcdef')。readline が IC 無し・im/ei 有りの端末
定義で 1 文字挿入に使う列 (ESC[4h + 空白 + ESC[4l + BS + 文字) で
'$ show ip route' の show の直後へ X を入れると、表示は '$ showXip route'
なのに、Enter で実行される行は '$ showX ip route' だった。SSH は
term='vt100' 固定で vt100 の terminfo に smir が無いので通常は届かないが、
シリアルでは相手の getty の端末定義しだいで届く。
tests/test_terminal_fast_paths.py の比較は近道と 1 文字ずつの経路の
突き合わせなので、両方が無視している間は検出できなかった。

直し方: CSI 4 h / 4 l で insert_mode を切り替える (reset で戻すので RIS
でも切れる)。挿入モードの間は印字を 1 文字ずつの経路へ回し、書き込む
直前に ICH と同じ処理でカーソルから右を文字の幅ぶん右へ送る。右端から
あふれた文字は消える (xterm と同じ)。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
KANJI = chr(0x6F22)
IRM_ON = ESC + "[4h"
IRM_OFF = ESC + "[4l"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class InsertModeTest(unittest.TestCase):
    def test_a_char_is_inserted_not_overwritten(self):
        """挿入モードでは、カーソルから右の文字が右へ送られること。"""
        s = feed(Screen(2, 10),
                 "abcdef" + ESC + "[3G" + IRM_ON + "X" + IRM_OFF)
        self.assertEqual(s.text(), ["abXcdef", ""],
                         "挿入モードなのに上書きしている")
        self.assertEqual(s.cursor_col, 3)

    def test_a_run_of_chars_is_inserted(self):
        """幅 1 の文字の連なりも、1 文字ずつ挿入されること。"""
        s = feed(Screen(2, 10), "abcdef" + ESC + "[3G" + IRM_ON + "XYZ")
        self.assertEqual(s.text(), ["abXYZcdef", ""])

    def test_readline_insert_with_im_and_ei(self):
        """readline が im/ei で空白を挿入してから書く列で、表示と行が揃うこと。"""
        s = feed(Screen(2, 20), "$ show ip route" + ESC + "[1;7H"
                 + IRM_ON + " " + IRM_OFF + "\b" + "X")
        self.assertEqual(s.text(), ["$ showX ip route", ""])

    def test_a_wide_char_is_inserted(self):
        """全角は 2 セルぶん右へ送って挿入されること。"""
        s = feed(Screen(2, 10), "abcdef" + ESC + "[3G" + IRM_ON + KANJI)
        self.assertEqual(s.text(), ["ab" + KANJI + "cdef", ""])
        self.assertEqual(s.cursor_col, 4)

    def test_chars_pushed_past_the_right_edge_are_lost(self):
        """右端からあふれた文字は消え、全角の片割れも残らないこと。"""
        s = feed(Screen(2, 6), "abcdef" + ESC + "[1G" + IRM_ON + "X")
        self.assertEqual(s.text(), ["Xabcde", ""])
        s = feed(Screen(2, 6), "abcd" + KANJI + ESC + "[1G" + IRM_ON + "X")
        self.assertEqual(s.text(), ["Xabcd", ""])

    def test_reset_mode_overwrites_again(self):
        """ESC[4l のあとは、また上書きに戻ること。"""
        s = feed(Screen(2, 10), "abcdef" + ESC + "[3G" + IRM_ON + "X"
                 + IRM_OFF + "Y")
        self.assertEqual(s.text(), ["abXYdef", ""])

    def test_full_reset_turns_insert_mode_off(self):
        """RIS (ESC c) で挿入モードが切れること。"""
        s = feed(Screen(2, 10), IRM_ON + ESC + "c" + "abc" + ESC + "[1G"
                 + "X")
        self.assertEqual(s.text(), ["Xbc", ""])

    def test_private_mode_4_does_not_turn_insert_mode_on(self):
        """私用モードの ESC[?4h (DECSCLM) は挿入モードと取り違えないこと。"""
        s = feed(Screen(2, 10), "abcdef" + ESC + "[3G" + ESC + "[?4h" + "X")
        self.assertEqual(s.text(), ["abXdef", ""])


if __name__ == "__main__":
    unittest.main()
