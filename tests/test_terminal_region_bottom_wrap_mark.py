r"""行の挿入 (IL) と下向きスクロール (SD) が、スクロール範囲の下端へ
上がってきた行の折り返しの印を置き去りにする件を検証する。

5 周目で範囲の上端側 (_drop_mark_above) は直ったが、下端側は対称に
なっていなかった。IL と SD は範囲の下端の行を捨ててから上端側へ 1 行
ぶん詰めるので、下端へ来るのは「捨てられた行の 1 つ上の行」になる。
その行の続きは今しがた捨てた行だったのに印がそのまま残るため、範囲の
下端が画面の下端より上のとき (DECSTBM で最下行をステータス行として
範囲外に残す画面) に、範囲の中の行と範囲外の行が 1 行に繋がる。

実測 (基準 f83be17): Screen(4, 4) に ESC[1;3r ESC[4;1H 'STAT'
ESC[2;1H 'ABCDEFGH' を流すと rows=['','ABCD','EFGH','STAT']・
wrapped=[False,True,False,False]。ここへ ESC[1;1H ESC[L (IL 1) か
ESC[T (SD 1) を流すと rows=['','','ABCD','STAT'] で wrapped は
[False,False,True,False]。続けて ESC[r で範囲を戻して画面を流すと、
押し出した履歴が [..., ('ABCD', True), ('STAT', False)] になり、描画側が
無関係な 2 つの論理行を 'ABCDSTAT' の 1 行に繋いだ (期待は 2 行)。

直し方: 上端側と対称に、下端の行の印も外す。_shift_lines (insert=True)
と _scroll_down は、範囲の下端の行の続きを範囲の外へ置いたまま捨てて
いるので、下端へ上がってきた行に続きはもう無い。DL は下端へ空行を
入れる側で印が最初から立たないため触らない。_scroll_up も下端へ空行を
入れる側で、そこの手前の印は最下行での折り返し (_linefeed(from_wrap=
True)) が使うので、これまでどおり残す。
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


def status_line_sample(rows=4, cols=4):
    """最下行を範囲外のステータス行にした画面を作る。

    範囲は 1 行目から 3 行目まで。範囲の下端 (3 行目) は 2 行目からの
    折り返しの続きで、4 行目は範囲の外。
    """
    s = feed(Screen(rows, cols), ESC + "[1;%dr" % (rows - 1)
             + ESC + "[%d;1H" % rows + "STAT"
             + ESC + "[%d;1H" % (rows - 2) + "ABCDEFGH")
    assert s.wrapped[rows - 3], "前提: 範囲の下端の 1 つ上に印が無い"
    return s


class RegionBottomWrapMarkTest(unittest.TestCase):
    def test_the_sample_screen_starts_wrapped(self):
        """出発点の画面が、範囲の中だけで折り返していること。"""
        s = status_line_sample()
        self.assertEqual(s.text(), ["", "ABCD", "EFGH", "STAT"])
        self.assertEqual(s.wrapped, [False, True, False, False])

    def test_insert_line_drops_the_mark_at_the_region_bottom(self):
        """IL で続きを捨てたら、範囲の下端の行の印が外れること。"""
        s = feed(status_line_sample(), ESC + "[1;1H" + ESC + "[L")
        self.assertEqual(s.text(), ["", "", "ABCD", "STAT"])
        self.assertFalse(s.wrapped[2],
                         "続きを捨てた行に折り返しの印が残っている")

    def test_scroll_down_drops_the_mark_at_the_region_bottom(self):
        """SD で続きを捨てたら、範囲の下端の行の印が外れること。"""
        s = feed(status_line_sample(), ESC + "[T")
        self.assertEqual(s.text(), ["", "", "ABCD", "STAT"])
        self.assertFalse(s.wrapped[2],
                         "続きを捨てた行に折り返しの印が残っている")

    def test_reverse_index_drops_the_mark_at_the_region_bottom(self):
        """範囲の上端での RI (ESC M) でも、下端の行の印が外れること。"""
        s = feed(status_line_sample(), ESC + "[1;1H" + ESC + "M")
        self.assertEqual(s.text(), ["", "", "ABCD", "STAT"])
        self.assertFalse(s.wrapped[2],
                         "続きを捨てた行に折り返しの印が残っている")

    def test_insert_line_does_not_join_the_status_row_in_history(self):
        """IL のあと、範囲外の行が履歴で繋がらないこと。"""
        s = feed(status_line_sample(), ESC + "[1;1H" + ESC + "[L"
                 + ESC + "[r" + ESC + "[4;1H" + "\r\n" * 4)
        self.assertEqual(history_rows(s)[2:4], [("ABCD", False),
                                                ("STAT", False)])

    def test_scroll_down_does_not_join_the_status_row_in_history(self):
        """SD のあと、範囲外の行が履歴で繋がらないこと。"""
        s = feed(status_line_sample(), ESC + "[T"
                 + ESC + "[r" + ESC + "[4;1H" + "\r\n" * 4)
        self.assertEqual(history_rows(s)[2:4], [("ABCD", False),
                                                ("STAT", False)])

    def test_the_alternate_screen_drops_the_mark_without_history(self):
        """代替画面でも印は外れ、履歴は増えないこと。"""
        s = feed(Screen(4, 4), ESC + "[?1049h" + ESC + "[1;3r"
                 + ESC + "[2;1H" + "ABCDEFGH" + ESC + "[T")
        self.assertTrue(s.alt_active)
        self.assertFalse(s.wrapped[2],
                         "続きを捨てた行に折り返しの印が残っている")
        self.assertEqual(history_rows(s), [])


class RegionBottomWrapMarkKeepsWorkingTest(unittest.TestCase):
    """素の折り返しと、通常のスクロール・履歴が変わらないこと。"""

    def test_plain_wrapping_still_marks_the_row(self):
        s = feed(Screen(4, 4), "ABCDX")
        self.assertEqual(s.text(), ["ABCD", "X", "", ""])
        self.assertTrue(s.wrapped[0], "折り返した行の印が消えている")

    def test_wrapping_at_the_last_row_still_marks_the_row(self):
        """最下行で折り返して画面が上がっても、印が残ること。"""
        s = feed(Screen(2, 4), ESC + "[2;1H" + "ABCDX")
        self.assertEqual(s.text(), ["ABCD", "X"])
        self.assertTrue(s.wrapped[0], "折り返しでスクロールした行の印が消えた")

    def test_wrapping_at_a_region_bottom_still_marks_the_row(self):
        """範囲の下端で折り返しても、範囲の中の印が残ること。"""
        s = feed(Screen(4, 4), ESC + "[1;3r" + ESC + "[3;1H" + "ABCDX")
        self.assertEqual(s.text(), ["", "ABCD", "X", ""])
        self.assertTrue(s.wrapped[1], "範囲内の折り返しの印が消えている")

    def test_scroll_up_keeps_the_mark_before_the_new_blank_row(self):
        """SU で下端へ空行が入っても、その手前の印は残ること。"""
        s = feed(Screen(4, 4), ESC + "[1;3r" + ESC + "[3;1H" + "ABCDX"
                 + ESC + "[S")
        self.assertEqual(s.text(), ["ABCD", "X", "", ""])
        self.assertTrue(s.wrapped[0], "折り返しの印が SU で消えている")

    def test_delete_line_keeps_a_wrapped_pair_inside_the_region(self):
        """DL で範囲の中に残った折り返しの組は、印が残ること。"""
        s = feed(Screen(4, 4), "ZZ\r\n" + "ABCDX" + ESC + "[1;1H" + ESC + "[M")
        self.assertEqual(s.text(), ["ABCD", "X", "", ""])
        self.assertTrue(s.wrapped[0], "範囲内に残った折り返しの印が消えた")

    def test_a_full_screen_region_still_records_history(self):
        """範囲を狭めない SU は、これまでどおり履歴へ送ること。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[S")
        self.assertEqual(history_rows(s), [("ABCD", True)])


class RegionBottomWrapMarkThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_status_row_apart(self):
        """受信の経路でも、範囲外の行が前の行と繋がらないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        rows, cols = terminal._screen.rows, terminal._screen.cols
        head = "1" * cols
        text = (ESC + "[1;%dr" % (rows - 1)
                + ESC + "[%d;1H" % rows + "STAT"
                + ESC + "[%d;1H" % (rows - 2) + head * 2
                + ESC + "[1;1H" + ESC + "[L"
                + ESC + "[r" + ESC + "[%d;1H" % rows + "\r\n" * rows)
        w.queue_output("dev", text)
        w._flush_pending_output()
        self.assertNotIn(head + "STAT", terminal.toPlainText(),
                         "範囲外の行が前の行と 1 行に繋がっている")


if __name__ == "__main__":
    unittest.main()
