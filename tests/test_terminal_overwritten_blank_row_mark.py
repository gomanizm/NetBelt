r"""空白で塗り潰して消した行から、折り返しの印が外れることを検証する。

EL (_erase_line) と DCH / ICH (_shift_chars) は、消したあとの行が丸ごと
BLANK なら折り返しの印を外す。しかし印字の経路 (_print_chars /
_print_narrow) は「入ってきたときの印が立っていて、書き直しが右端まで
届いたら印を残す」だけで、書いた中身が空白だけかを見ていなかった。EL を
使わず空白の上書きで行を消すアプリでは、印が残ったままになる。

実測 (基準 8b0c94e): Screen(4, 4) に 'ABCDX' ESC[1;1H '    ' を流すと、
第 1 行は全セル空白なのに wrapped[0] は True のまま。続けて ESC[4;1H
'\r\n\r\n' で押し出すと take_new_history は [('', True), ('X', False)]
で、履歴・コピー・文書では空行が消えて次の行と 1 行に繋がる。ESC(0 の
罫線用文字集合 (_print_chars) でも、挿入モード (IRM) でも同じ。
TerminalWidget では 4x8 に '12345678NEXT' ESC[1;1H 空白 8 個 ESC[4;1H
改行 4 回で、文書が ['        NEXT', '', '', ''] になった (期待は
['', 'NEXT', '', ''])。

直し方: _print_chars と _print_narrow の印の代入に、_erase_line /
_shift_chars と同じ空行判定 (and not all(c == BLANK for c in line)) を
足す。印が True になるときしか all() を評価しないので、速い道は変わら
ない。色の付いた空白は BLANK ではないので中身として扱い、印は残る。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
GRAPHICS_ON = ESC + "(0"
GRAPHICS_OFF = ESC + "(B"
INSERT_ON = ESC + "[4h"
INSERT_OFF = ESC + "[4l"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def history_rows(screen):
    return [("".join(cell[0] for cell in line).rstrip(), wrapped)
            for line, wrapped in screen.take_new_history()]


class OverwrittenBlankRowMarkTest(unittest.TestCase):
    def test_blanking_a_row_with_spaces_drops_the_wrap_mark(self):
        """空白でちょうど右端まで塗り潰したら、印が外れること。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[1;1H" + "    ")
        self.assertEqual(s.text(), ["", "X", "", ""])
        self.assertFalse(s.wrapped[0],
                         "空白だけになった行に折り返しの印が残っている")

    def test_blanking_through_the_graphics_charset_drops_the_mark(self):
        """1 文字ずつ書く経路 (ESC(0) でも、印が外れること。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[1;1H"
                 + GRAPHICS_ON + "    " + GRAPHICS_OFF)
        self.assertEqual(s.text(), ["", "X", "", ""])
        self.assertFalse(s.wrapped[0],
                         "空白だけになった行に折り返しの印が残っている")

    def test_blanking_in_insert_mode_drops_the_mark(self):
        """挿入モードで中身を押し出しきっても、印が外れること。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[1;1H"
                 + INSERT_ON + "    " + INSERT_OFF)
        self.assertEqual(s.text(), ["", "X", "", ""])
        self.assertFalse(s.wrapped[0],
                         "空白だけになった行に折り返しの印が残っている")

    def test_the_blanked_row_is_not_joined_in_history(self):
        """押し出した履歴で、塗り潰した行が次の行と繋がらないこと。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[1;1H" + "    "
                 + ESC + "[4;1H\r\n\r\n")
        self.assertEqual(history_rows(s), [("", False), ("X", False)])

    def test_plain_wrapping_still_marks_the_row(self):
        """素の折り返しでは、これまでどおり印が付くこと。"""
        s = feed(Screen(4, 4), "ABCDX")
        self.assertEqual(s.text(), ["ABCD", "X", "", ""])
        self.assertTrue(s.wrapped[0], "折り返した行の印が消えている")

    def test_rewriting_to_the_right_edge_keeps_the_mark(self):
        """右端まで届く書き直しでは、中身があれば印が残ること。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[1;1H" + "WXYZ")
        self.assertEqual(s.text(), ["WXYZ", "X", "", ""])
        self.assertTrue(s.wrapped[0], "書き直した折り返し行の印が消えている")

    def test_a_space_left_of_content_keeps_the_mark(self):
        """空白が一部でも、中身が残る行の印は外さないこと。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[1;1H" + "   Z")
        self.assertEqual(s.text(), ["   Z", "X", "", ""])
        self.assertTrue(s.wrapped[0], "中身の残る行の印が消えている")

    def test_coloured_blanks_are_content_and_keep_the_mark(self):
        """色の付いた空白は中身なので、印が残ること。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[1;1H"
                 + ESC + "[41m" + "    " + ESC + "[0m")
        self.assertEqual(s.text(), ["", "X", "", ""])
        self.assertTrue(s.wrapped[0], "色の付いた空白の行の印が消えている")


class OverwrittenBlankRowThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_rows_apart(self):
        """受信の経路でも、塗り潰した行と次の行が繋がらないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        rows, cols = terminal._screen.rows, terminal._screen.cols
        text = ("1" * cols + "NEXT"
                + ESC + "[1;1H" + " " * cols
                + ESC + "[%d;1H" % rows + "\r\n" * rows)
        w.queue_output("dev", text)
        w._flush_pending_output()
        lines = terminal.toPlainText().split("\n")
        self.assertEqual([line.rstrip() for line in lines[:2]], ["", "NEXT"],
                         "塗り潰した行と次の行が 1 行に繋がっている")


if __name__ == "__main__":
    unittest.main()
