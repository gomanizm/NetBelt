r"""EL 1 のあとに印字された空白しか残らない行から、折り返しの印を外すことを検証する。

_erase_line は、消した範囲 (全角で広がった分を含む) が行末まで届いたとき
だけ折り返しの印を外していた。消した範囲より右に中身が残っていれば印を
残すのは正しいが、そこに残っているのが印字された空白 (BLANK と同じ既定
属性の空白) だけだと、行は丸ごと空白なのに印が残った。

実測: Screen(4, 4) に 'AB  X' ESC[1;2H ESC[1K を流すと、第 1 行は全セル
空白なのに wrapped[0] は True のまま。続けて ESC[4;1H '\r\n\r\n' で押し
出すと、take_new_history は [('    ', True), ('X   ', False)] で、履歴・
コピー・文書では '    X' の 1 行に繋がった (空行が消え、X の前に空白が
混ざる)。全角の例 ('界  X' ESC[1;1H ESC[1K) も同じく印が残った。

直し方: 消した範囲が行末まで届いたときに加えて、消したあとの行が丸ごと
BLANK なら印を外す。DCH / ICH (_shift_chars) が行が空になったと判断する
基準と同じ。色の付いた空白は BLANK ではないので中身として扱い、印を残す。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
WIDE = chr(0x754C)      # 界


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def history_rows(screen):
    return [("".join(cell[0] for cell in line).rstrip(), wrapped)
            for line, wrapped in screen.take_new_history()]


class EraseLeavingPrintedBlanksTest(unittest.TestCase):
    def test_row_left_with_printed_blanks_loses_its_wrap_mark(self):
        """EL 1 の右に印字された空白しか残らなければ、印が外れること。"""
        s = feed(Screen(4, 4), "AB  X" + ESC + "[1;2H" + ESC + "[1K")
        self.assertEqual(s.text(), ["", "X", "", ""])
        self.assertFalse(s.wrapped[0],
                         "空白だけになった行に折り返しの印が残っている")

    def test_wide_char_row_left_with_printed_blanks_loses_its_wrap_mark(self):
        """全角を消して印字された空白しか残らなくても、印が外れること。"""
        s = feed(Screen(4, 4), WIDE + "  X" + ESC + "[1;1H" + ESC + "[1K")
        self.assertEqual(s.text(), ["", "X", "", ""])
        self.assertFalse(s.wrapped[0],
                         "空白だけになった行に折り返しの印が残っている")

    def test_the_blank_row_is_not_joined_in_history(self):
        """押し出した履歴で、空白だけの行が次の行と繋がらないこと。"""
        s = feed(Screen(4, 4), "AB  X" + ESC + "[1;2H" + ESC + "[1K"
                 + ESC + "[4;1H\r\n\r\n")
        self.assertEqual(history_rows(s), [("", False), ("X", False)])

    def test_content_right_of_the_erased_range_keeps_the_mark(self):
        """消した範囲の右に中身が残るなら、折り返しの印は残すこと。"""
        s = feed(Screen(4, 5), "AB  CX" + ESC + "[1;2H" + ESC + "[1K")
        self.assertEqual(s.text(), ["    C", "X", "", ""])
        self.assertTrue(s.wrapped[0])


class EraseLeavingPrintedBlanksThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_rows_apart(self):
        """受信の経路でも、文書で空白だけの行と次の行が繋がらないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        rows, cols = terminal._screen.rows, terminal._screen.cols
        text = ("AB" + " " * (cols - 2) + "X"
                + ESC + "[1;2H" + ESC + "[1K"
                + ESC + "[%d;1H" % rows + "\r\n" * rows)
        w.queue_output("dev", text)
        w._flush_pending_output()
        lines = terminal.toPlainText().split("\n")
        self.assertEqual([line.rstrip() for line in lines[:2]], ["", "X"],
                         "空白だけの行と次の行が 1 行に繋がっている")


if __name__ == "__main__":
    unittest.main()
