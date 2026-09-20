r"""行の挿入・削除 (IL/DL) と、上端が 0 でない範囲のスクロール (SU/SD)
が、1 つ上の行の折り返しの印を置き去りにする件を検証する。

折り返しの印 (wrapped[r]) は「この行は次の行へ続く」という意味なので、
行が動いて r+1 に別の論理行 (または新しい空行) が来たら外さなければ
ならない。_shift_lines (IL/DL) と _scroll_up / _scroll_down は行と印を
一緒に動かすだけで、動いた先の 1 つ上の行の印を見ていなかった。

実測 (基準 ac1dee7): Screen(4, 4) に 'ABCDX' ESC[3;1H 'YZ' を流すと
rows=['ABCD','X','YZ','']・wrapped=[True,False,False,False]。ここへ
ESC[2;1H ESC[M (DL 1) を流すと rows=['ABCD','YZ','','']、wrapped[0] は
True のままで、押し出したときの履歴は [('ABCD', True), ('YZ', False)]。
描画側は印の立った行を改行で切らずに次と繋ぐので、無関係な 2 つの論理行
が 'ABCDYZ' の 1 行になる (期待は 2 行)。IL (ESC[L)、上端が 0 でない
範囲での SU (ESC[S)・SD (ESC[T)・RI (ESC M) も同じ。TerminalWidget の
受信の経路でも同じ文書ができた。

直し方: _shift_lines は cursor_row に、_scroll_up / _scroll_down は
scroll_top に別の行 (または空行) が来るので、その 1 つ上の行の印を外す。
_scroll_up の下端へ入る空行の手前は外さない。折り返しで最下行から
スクロールするとき (_linefeed(from_wrap=True)) の印がそこに載っており、
外すと素の折り返しが切れる。画面の上端が押し出される既存の経路
(履歴へ送る側) は触らない。
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


def wrapped_sample(rows=4, cols=4):
    """['ABCD'(折り返し), 'X', 'YZ', ''] の画面を作る。"""
    return feed(Screen(rows, cols), "ABCDX" + ESC + "[3;1H" + "YZ")


class ShiftedRowWrapMarkTest(unittest.TestCase):
    def test_the_sample_screen_starts_wrapped(self):
        """出発点の画面が、折り返しの印を 1 つだけ持つこと。"""
        s = wrapped_sample()
        self.assertEqual(s.text(), ["ABCD", "X", "YZ", ""])
        self.assertEqual(s.wrapped, [True, False, False, False])

    def test_delete_line_drops_the_mark_above(self):
        """DL で続きが消えたら、1 つ上の行の印が外れること。"""
        s = feed(wrapped_sample(), ESC + "[2;1H" + ESC + "[M")
        self.assertEqual(s.text(), ["ABCD", "YZ", "", ""])
        self.assertFalse(s.wrapped[0],
                         "続きが消えた行に折り返しの印が残っている")

    def test_insert_line_drops_the_mark_above(self):
        """IL で空行が割り込んだら、1 つ上の行の印が外れること。"""
        s = feed(wrapped_sample(), ESC + "[2;1H" + ESC + "[L")
        self.assertEqual(s.text(), ["ABCD", "", "X", "YZ"])
        self.assertFalse(s.wrapped[0],
                         "空行が割り込んだ行に折り返しの印が残っている")

    def test_delete_of_several_lines_drops_the_mark_above(self):
        """DL 2 のように複数行まとめて消しても、印が外れること。"""
        s = feed(wrapped_sample(), ESC + "[2;1H" + ESC + "[2M")
        self.assertEqual(s.text(), ["ABCD", "", "", ""])
        self.assertFalse(s.wrapped[0],
                         "続きが消えた行に折り返しの印が残っている")

    def test_scroll_up_in_a_lower_region_drops_the_mark_above(self):
        """上端が 0 でない範囲の SU で、範囲の 1 つ上の印が外れること。"""
        s = feed(wrapped_sample(), ESC + "[2;4r" + ESC + "[S")
        self.assertEqual(s.text(), ["ABCD", "YZ", "", ""])
        self.assertFalse(s.wrapped[0],
                         "続きがスクロールで消えた行に印が残っている")

    def test_scroll_down_in_a_lower_region_drops_the_mark_above(self):
        """上端が 0 でない範囲の SD で、範囲の 1 つ上の印が外れること。"""
        s = feed(wrapped_sample(), ESC + "[2;4r" + ESC + "[T")
        self.assertEqual(s.text(), ["ABCD", "", "X", "YZ"])
        self.assertFalse(s.wrapped[0],
                         "空行が割り込んだ行に折り返しの印が残っている")

    def test_reverse_index_at_a_lower_region_top_drops_the_mark(self):
        """範囲の上端での RI (ESC M) でも、1 つ上の印が外れること。"""
        s = feed(wrapped_sample(), ESC + "[2;4r" + ESC + "[2;1H" + ESC + "M")
        self.assertEqual(s.text(), ["ABCD", "", "X", "YZ"])
        self.assertFalse(s.wrapped[0],
                         "空行が割り込んだ行に折り返しの印が残っている")

    def test_the_rows_are_not_joined_in_history(self):
        """押し出した履歴で、DL のあとの 2 行が繋がらないこと。"""
        s = feed(wrapped_sample(), ESC + "[2;1H" + ESC + "[M"
                 + ESC + "[4;1H" + "\r\n" * 4)
        self.assertEqual(history_rows(s)[:2], [("ABCD", False),
                                               ("YZ", False)])


class ShiftedRowWrapMarkKeepsWorkingTest(unittest.TestCase):
    """折り返しそのものと、既存のスクロール・履歴が変わらないこと。"""

    def test_plain_wrapping_still_marks_the_row(self):
        """素の折り返しでは、これまでどおり印が付くこと。"""
        s = feed(Screen(4, 4), "ABCDX")
        self.assertEqual(s.text(), ["ABCD", "X", "", ""])
        self.assertTrue(s.wrapped[0], "折り返した行の印が消えている")

    def test_wrapping_at_the_last_row_still_marks_the_row(self):
        """最下行で折り返して画面が上がっても、印が残ること。"""
        s = feed(Screen(2, 4), ESC + "[2;1H" + "ABCDX")
        self.assertEqual(s.text(), ["ABCD", "X"])
        self.assertTrue(s.wrapped[0], "折り返しでスクロールした行の印が消えた")

    def test_wrapping_at_a_lower_region_bottom_still_marks_the_row(self):
        """上端が 0 でない範囲の最下行で折り返しても、印が残ること。"""
        s = feed(Screen(3, 4), ESC + "[2;3r" + ESC + "[3;1H" + "ABCDX")
        self.assertEqual(s.text(), ["", "ABCD", "X"])
        self.assertTrue(s.wrapped[1], "範囲内の折り返しの印が消えている")

    def test_history_keeps_the_mark_of_a_wrapped_row(self):
        """画面の上端から押し出された折り返し行は、印ごと記録されること。"""
        s = feed(Screen(2, 4), "ABCDX" + "\r\n")
        self.assertEqual(history_rows(s), [("ABCD", True)])

    def test_delete_line_at_the_top_still_records_history(self):
        """画面の先頭での DL は、これまでどおり履歴へ送ること。"""
        s = feed(Screen(3, 4), "AB\r\nCD\r\nEF" + ESC + "[1;1H" + ESC + "[M")
        self.assertEqual(s.text(), ["CD", "EF", ""])
        self.assertEqual(history_rows(s), [("AB", False)])

    def test_scrolling_with_a_region_from_the_top_is_unchanged(self):
        """上端が 0 の範囲でのスクロールは、これまでどおりであること。"""
        s = feed(Screen(4, 4), ESC + "[1;3r" + "ABCDX" + ESC + "[S")
        self.assertEqual(s.text(), ["X", "", "", ""])
        self.assertEqual(history_rows(s), [("ABCD", True)])

    def test_alternate_screen_shift_drops_the_mark_without_history(self):
        """代替画面でも印は外れ、履歴は増えないこと。"""
        s = feed(Screen(4, 4), ESC + "[?1049h" + "ABCDX"
                 + ESC + "[2;1H" + ESC + "[M")
        self.assertTrue(s.alt_active)
        self.assertFalse(s.wrapped[0],
                         "続きが消えた行に折り返しの印が残っている")
        self.assertEqual(history_rows(s), [])

    def test_the_main_screen_marks_survive_the_alternate_screen(self):
        """代替画面で動かしても、裏のメイン画面の印が残ること。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[?1049h"
                 + ESC + "[1;1H" + ESC + "[M" + ESC + "[?1049l")
        self.assertEqual(s.text(), ["ABCD", "X", "", ""])
        self.assertTrue(s.wrapped[0], "メイン画面の折り返しの印が消えている")


class ShiftedRowWrapMarkThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_rows_apart(self):
        """受信の経路でも、DL のあとの 2 行が繋がらないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        rows, cols = terminal._screen.rows, terminal._screen.cols
        head = "1" * cols
        text = (head + "NEXT"
                + ESC + "[3;1H" + "TAIL"
                + ESC + "[2;1H" + ESC + "[M"
                + ESC + "[%d;1H" % rows + "\r\n" * rows)
        w.queue_output("dev", text)
        w._flush_pending_output()
        lines = terminal.toPlainText().split("\n")
        self.assertEqual([line.rstrip() for line in lines[:2]],
                         [head, "TAIL"],
                         "続きの消えた行が次の行と 1 行に繋がっている")


if __name__ == "__main__":
    unittest.main()
