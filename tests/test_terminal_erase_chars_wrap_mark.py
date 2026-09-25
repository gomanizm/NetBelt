r"""ECH (ESC[X) だけが、中身が残っていても折り返しの印を外す件を検証する。

_erase_chars は消したあとの行を見ずに wrapped[cursor_row] = False として
いた (注釈は「印字と同じ扱い」)。4 周目で印字の側が「右端まで届いても
結果が空白だけなら外す・中身が残れば保つ」へ変わったので、この注釈は
現状と食い違い、ECH だけが 1 本の論理行を履歴・コピー・文書で 2 行に
割るようになっていた。

実測 (基準 ac1dee7): Screen(3, 4) に 'ABCDX' ESC[1;2H ESC[1X を流すと
text=['A CD','X','']・wrapped は全部 False。同じ位置の EL 1 (ESC[1K)・
DCH (ESC[1P) と、同じ結果になる素の書き直し (ESC[1;2H ' CD') はどれも
印を保つので、ECH だけが外れていた。押し出すと履歴は
[('A CD', False), ('X', False)] で、TerminalWidget の文書でも 1 行だった
はずの出力が 2 行に割れた。

直し方: ECH も EL / DCH / ICH と同じ基準 (行が丸ごと BLANK なら外す、
中身が残れば保つ) に揃える。あわせて screen.py 冒頭の docstring の
「折り返しの印」の段落と、?1048 / ?1049 の保存領域の注釈を実装に合わせる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def history_rows(screen):
    return [("".join(cell[0] for cell in line).rstrip(), wrapped)
            for line, wrapped in screen.take_new_history()]


def wrapped_row(rows=3, cols=4):
    """['ABCD'(折り返し), 'X', ''] の画面を作る。"""
    s = feed(Screen(rows, cols), "ABCDX")
    assert s.wrapped[0], "前提: 折り返しの印が付いていない"
    return s


class EraseCharsWrapMarkTest(unittest.TestCase):
    def test_erasing_part_of_the_row_keeps_the_mark(self):
        """中身が残る ECH では、折り返しの印が残ること。"""
        s = feed(wrapped_row(), ESC + "[1;2H" + ESC + "[1X")
        self.assertEqual(s.text(), ["A CD", "X", ""])
        self.assertTrue(s.wrapped[0], "中身の残る行の折り返しの印が外れた")

    def test_erasing_to_the_row_end_keeps_the_mark(self):
        """行末まで消しても、左に中身が残れば印が残ること。"""
        s = feed(wrapped_row(), ESC + "[1;3H" + ESC + "[9X")
        self.assertEqual(s.text(), ["AB", "X", ""])
        self.assertTrue(s.wrapped[0], "中身の残る行の折り返しの印が外れた")

    def test_erasing_the_whole_row_drops_the_mark(self):
        """行が丸ごと空白になったら、これまでどおり印が外れること。"""
        s = feed(wrapped_row(), ESC + "[1;1H" + ESC + "[4X")
        self.assertEqual(s.text(), ["", "X", ""])
        self.assertFalse(s.wrapped[0], "空になった行に折り返しの印が残っている")

    def test_the_rows_stay_joined_in_history(self):
        """押し出した履歴で、1 本の論理行が割れないこと。"""
        s = feed(wrapped_row(), ESC + "[1;2H" + ESC + "[1X"
                 + ESC + "[3;1H" + "\r\n" * 3)
        self.assertEqual(history_rows(s)[:2], [("A CD", True), ("X", False)])


class EraseCharsMatchesTheOthersTest(unittest.TestCase):
    """同じ位置の EL 1・DCH・素の書き直しと、判断が揃っていること。"""

    def test_erase_line_1_keeps_the_mark(self):
        s = feed(wrapped_row(), ESC + "[1;2H" + ESC + "[1K")
        self.assertEqual(s.text(), ["  CD", "X", ""])
        self.assertTrue(s.wrapped[0], "EL 1 で中身の残る行の印が外れた")

    def test_delete_character_keeps_the_mark(self):
        s = feed(wrapped_row(), ESC + "[1;2H" + ESC + "[1P")
        self.assertEqual(s.text(), ["ACD", "X", ""])
        self.assertTrue(s.wrapped[0], "DCH で中身の残る行の印が外れた")

    def test_printing_the_same_result_keeps_the_mark(self):
        """ECH と同じ見た目を印字で作った場合と、結果が一致すること。"""
        s = feed(wrapped_row(), ESC + "[1;2H" + " CD")
        self.assertEqual(s.text(), ["A CD", "X", ""])
        self.assertTrue(s.wrapped[0], "書き直しで中身の残る行の印が外れた")

    def test_printing_blanks_over_the_whole_row_drops_the_mark(self):
        """行を空白で塗り潰した場合と、結果が一致すること。"""
        s = feed(wrapped_row(), ESC + "[1;1H" + "    ")
        self.assertEqual(s.text(), ["", "X", ""])
        self.assertFalse(s.wrapped[0], "空になった行に折り返しの印が残っている")


class EraseCharsWrapMarkThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_logical_line_together(self):
        """受信の経路でも、ECH のあとに行が割れないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        rows, cols = terminal._screen.rows, terminal._screen.cols
        text = ("1" * cols + "NEXT"
                + ESC + "[1;2H" + ESC + "[1X"
                + ESC + "[%d;1H" % rows + "\r\n" * rows)
        w.queue_output("dev", text)
        w._flush_pending_output()
        lines = terminal.toPlainText().split("\n")
        self.assertEqual(lines[0], "1 " + "1" * (cols - 2) + "NEXT",
                         "ECH した行が次の行と切り離されている")


if __name__ == "__main__":
    unittest.main()
