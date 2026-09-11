"""全角・結合文字・ゼロ幅文字が占めるセル数を検証する。

画面モデルはどの文字も 1 セルとして進めていた。実端末 (xterm) は
東アジア幅 W/F の文字を 2 セル、結合文字と ZWJ 等の書式文字を幅 0 と
して扱うので、全角のあとの CUP や DSR 応答が 1 桁ずつずれ、結合文字
だけが次の行へ折り返された。機器 CLI (ASCII) では出ないが、Linux 側
で日本語を含む行を readline や vim で編集すると表示が崩れる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

WIDE = "界"                 # 界 (東アジア幅 W)
ACUTE = "́"                # 結合アキュート
ZWJ = "‍"
EMOJI = "\U0001F600"            # 😀 (W、BMP 外)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class WideCharacterTest(unittest.TestCase):
    def test_a_wide_character_takes_two_cells(self):
        s = feed(Screen(rows=2, cols=10), WIDE)
        self.assertEqual(s.cursor_col, 2, "全角のあとのカーソルが 1 桁分しか進んでいない")

    def test_cup_after_a_wide_character_lands_on_the_right_column(self):
        # 実端末なら 3 桁目は X の位置なので、Z が X を上書きする
        s = feed(Screen(rows=2, cols=10), WIDE + "X\x1b[3GZ")
        self.assertEqual(s.text()[0], WIDE + "Z")

    def test_an_emoji_is_wide_too(self):
        s = feed(Screen(rows=2, cols=10), EMOJI + "X\x1b[3GZ")
        self.assertEqual(s.text()[0], EMOJI + "Z")

    def test_the_cursor_position_report_counts_cells(self):
        s = feed(Screen(rows=2, cols=10), WIDE + "\x1b[6n")
        self.assertEqual(s.take_responses(), ["\x1b[1;3R"])

    def test_a_wide_character_that_does_not_fit_wraps_whole(self):
        s = feed(Screen(rows=2, cols=3), "AB" + WIDE)
        self.assertEqual(s.text()[:2], ["AB", WIDE])
        self.assertTrue(s.wrapped[0])

    def test_overwriting_either_half_blanks_the_other(self):
        s = feed(Screen(rows=2, cols=10), WIDE + "\x1b[1GX")
        self.assertEqual(s.text()[0], "X")
        self.assertEqual(len("".join(c[0] for c in s.lines[0])), 10,
                         "継続セルが残って桁が 1 つ消えている")
        s = feed(Screen(rows=2, cols=10), WIDE + "\x1b[2GX")
        self.assertEqual(s.text()[0], " X")

    def test_erasing_half_of_a_wide_character_erases_it_whole(self):
        s = feed(Screen(rows=2, cols=10), WIDE + "A\x1b[2G\x1b[1X")
        self.assertEqual(s.text()[0], "  A")
        s = feed(Screen(rows=2, cols=10), "A" + WIDE + "\x1b[3G\x1b[1K")
        self.assertEqual(s.text()[0], "")

    def test_a_wide_character_at_the_edge_sets_pending_wrap(self):
        s = feed(Screen(rows=2, cols=4), "AB" + WIDE + "C")
        self.assertEqual(s.text()[:2], ["AB" + WIDE, "C"])


class ZeroWidthCharacterTest(unittest.TestCase):
    def test_a_combining_mark_joins_the_previous_cell(self):
        s = feed(Screen(rows=2, cols=10), "e" + ACUTE + "X\x1b[2GZ")
        self.assertEqual(s.text()[0], "e" + ACUTE + "Z")

    def test_a_combining_mark_does_not_wrap_on_its_own(self):
        # 2 桁の画面に 'Aé' を書くと、アキュートだけが次の行に落ちていた
        s = feed(Screen(rows=2, cols=2), "Ae" + ACUTE)
        self.assertEqual(s.text()[:2], ["Ae" + ACUTE, ""])

    def test_a_zero_width_joiner_takes_no_cell(self):
        s = feed(Screen(rows=2, cols=10), "a" + ZWJ + "b\x1b[2GZ")
        self.assertEqual(s.text()[0], "a" + ZWJ + "Z")

    def test_a_combining_mark_after_a_wide_character_joins_it(self):
        s = feed(Screen(rows=2, cols=10), WIDE + ACUTE + "X")
        self.assertEqual(s.lines[0][0][0], WIDE + ACUTE)
        self.assertEqual(s.text()[0], WIDE + ACUTE + "X")

    def test_a_combining_mark_with_nothing_before_it_is_dropped(self):
        s = feed(Screen(rows=2, cols=10), ACUTE + "X")
        self.assertEqual(s.text()[0], "X")
        self.assertEqual(s.cursor_col, 1)


class NarrowTextUnchangedTest(unittest.TestCase):
    def test_ascii_still_advances_one_cell_per_character(self):
        s = feed(Screen(rows=2, cols=10), "show ver")
        self.assertEqual(s.cursor_col, 8)
        self.assertEqual(s.text()[0], "show ver")

    def test_line_drawing_is_one_cell(self):
        s = feed(Screen(rows=2, cols=10), "\x1b(0lqk\x1b(B")
        self.assertEqual(s.cursor_col, 3)


class CaretAfterAWideCharacterTest(unittest.TestCase):
    """表示側のキャレットも、桁ではなくセルで数えて置くこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_caret_sits_after_the_wide_character(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w._grid_size = lambda t: (10, 40)
        w.create_terminal_tab("dev")
        w._apply_grid_size("dev")
        terminal = w._terminals["dev"]
        w.append_output("dev", WIDE + "abc\x1b[3G")     # 'a' の上
        self.assertEqual(terminal._screen.cursor_col, 2)
        self.assertEqual(terminal.textCursor().positionInBlock(), 1,
                         "キャレットが全角のぶん右へずれている")


if __name__ == "__main__":
    unittest.main()
