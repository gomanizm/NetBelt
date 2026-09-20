r"""行末まで届く EL 0 だけが、中身の残る行から折り返しの印を外す件を検証する。

_erase_line は「消した範囲が行末まで届いたら印を外す」という判定を持って
いた。EL 0 (ESC[0K) は必ず行末まで届くので、左に中身が残っていても印が
外れる。ECH (ESC[X)・DCH (ESC[P)・右端まで空白を印字した書き直しは
4 周目以降「行が丸ごと空白なら外す」に揃っているため、同じ見た目の画面
から 2 通りの文書 (コピー結果) ができていた。

実測 (基準 f83be17): Screen(3, 4) に 'ABCDX' を流すと
rows=['ABCD','X','']・wrapped=[True,False,False]。
  ESC[1;3H ESC[9X  (ECH) → wrapped[0]=True、履歴 [('AB', True), ('X', False)]
  ESC[1;3H ESC[2P  (DCH) → wrapped[0]=True、履歴 [('AB', True), ('X', False)]
  ESC[1;3H '  '    (印字) → wrapped[0]=True、履歴 [('AB', True), ('X', False)]
  ESC[1;3H ESC[0K  (EL 0) → wrapped[0]=False、履歴 [('AB', False), ('X', False)]
EL 0 だけが 1 本の論理行を 'AB' と 'X' の 2 行に割っていた。

直し方 (2026-09-20 の決定): ほかに合わせ、行が丸ごと空白になったときだけ
印を外す規則へ統一する。EL 0 も文字消去・削除・印字と同じ扱いにし、コピー
では 1 論理行として繋がる。ED 0 (ESC[0J) はカーソル行から下をすべて空に
するので続きの行そのものが消える。こちらは _erase_display で明示的に印を
外し、これまでどおり 2 行のままにする。
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


class EraseToEndWrapMarkTest(unittest.TestCase):
    def test_erasing_to_the_row_end_keeps_the_mark(self):
        """EL 0 で中身が残るなら、折り返しの印が残ること。"""
        s = feed(wrapped_row(), ESC + "[1;3H" + ESC + "[0K")
        self.assertEqual(s.text(), ["AB", "X", ""])
        self.assertTrue(s.wrapped[0], "中身の残る行の折り返しの印が外れた")

    def test_the_rows_stay_joined_in_history(self):
        """押し出した履歴で、EL 0 のあとの 1 本の論理行が割れないこと。"""
        s = feed(wrapped_row(), ESC + "[1;3H" + ESC + "[0K"
                 + ESC + "[3;1H" + "\r\n" * 3)
        self.assertEqual(history_rows(s)[:2], [("AB", True), ("X", False)])

    def test_erasing_the_whole_row_drops_the_mark(self):
        """EL 0 で行が丸ごと空白になったら、印が外れること。"""
        s = feed(wrapped_row(), ESC + "[1;1H" + ESC + "[0K")
        self.assertEqual(s.text(), ["", "X", ""])
        self.assertFalse(s.wrapped[0], "空になった行に折り返しの印が残っている")

    def test_erasing_what_is_left_of_printed_blanks_drops_the_mark(self):
        """残るのが印字された空白だけなら、印が外れること。"""
        s = feed(Screen(3, 4), "  CDX" + ESC + "[1;3H" + ESC + "[0K")
        self.assertEqual(s.text(), ["", "X", ""])
        self.assertFalse(s.wrapped[0],
                         "空白だけになった行に折り返しの印が残っている")


class EraseToEndMatchesTheOthersTest(unittest.TestCase):
    """同じ見た目を作る ECH・DCH・印字と、判断が揃っていること。"""

    def _mark_after(self, rewrite):
        return feed(wrapped_row(), rewrite).wrapped[0]

    def test_all_the_ways_of_clearing_the_tail_agree(self):
        ways = {
            "EL 0": ESC + "[1;3H" + ESC + "[0K",
            "ECH": ESC + "[1;3H" + ESC + "[9X",
            "DCH": ESC + "[1;3H" + ESC + "[2P",
            "印字": ESC + "[1;3H" + "  ",
        }
        for name, rewrite in ways.items():
            with self.subTest(name):
                s = feed(wrapped_row(), rewrite)
                self.assertEqual(s.text(), ["AB", "X", ""])
                self.assertTrue(s.wrapped[0],
                                "%s で中身の残る行の印が外れた" % name)


class EraseToEndKeepsWorkingTest(unittest.TestCase):
    """ほかの消去命令が、これまでどおりであること。"""

    def test_erase_line_2_still_drops_the_mark(self):
        s = feed(wrapped_row(), ESC + "[1;3H" + ESC + "[2K")
        self.assertEqual(s.text(), ["", "X", ""])
        self.assertFalse(s.wrapped[0], "空になった行に折り返しの印が残っている")

    def test_erase_line_1_reaching_the_row_end_still_drops_the_mark(self):
        s = feed(wrapped_row(), ESC + "[1;4H" + ESC + "[1K")
        self.assertEqual(s.text(), ["", "X", ""])
        self.assertFalse(s.wrapped[0], "空になった行に折り返しの印が残っている")

    def test_erase_line_1_stopping_short_still_keeps_the_mark(self):
        s = feed(wrapped_row(), ESC + "[1;3H" + ESC + "[1K")
        self.assertEqual(s.text(), ["   D", "X", ""])
        self.assertTrue(s.wrapped[0], "中身の残る行の折り返しの印が外れた")

    def test_erase_display_0_still_drops_the_mark(self):
        """ED 0 は続きの行ごと消すので、これまでどおり印が外れること。"""
        s = feed(wrapped_row(), ESC + "[1;3H" + ESC + "[0J")
        self.assertEqual(s.text(), ["AB", "", ""])
        self.assertFalse(s.wrapped[0], "続きを消した行に折り返しの印が残っている")

    def test_erase_display_0_does_not_join_later_output(self):
        """ED 0 のあとに来た出力が、消し残った行と繋がらないこと。"""
        s = feed(wrapped_row(), ESC + "[1;3H" + ESC + "[0J"
                 + ESC + "[2;1H" + "NEW"
                 + ESC + "[3;1H" + "\r\n" * 3)
        self.assertEqual(history_rows(s)[:2], [("AB", False), ("NEW", False)])

    def test_erase_display_2_still_drops_the_mark(self):
        s = feed(wrapped_row(), ESC + "[1;3H" + ESC + "[2J")
        self.assertEqual(s.wrapped, [False, False, False])

    def test_erase_display_1_keeps_a_row_with_content(self):
        """ED 1 はカーソル行の下を触らないので、続きの行は続きのまま。"""
        s = feed(wrapped_row(), ESC + "[1;3H" + ESC + "[1J")
        self.assertEqual(s.text(), ["   D", "X", ""])
        self.assertTrue(s.wrapped[0], "中身の残る行の折り返しの印が外れた")


class EraseToEndWrapMarkThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_logical_line_together(self):
        """受信の経路でも、EL 0 のあとに行が割れないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        rows, cols = terminal._screen.rows, terminal._screen.cols
        text = ("1" * cols + "NEXT"
                + ESC + "[1;%dH" % cols + ESC + "[0K"
                + ESC + "[%d;1H" % rows + "\r\n" * rows)
        w.queue_output("dev", text)
        w._flush_pending_output()
        lines = terminal.toPlainText().split("\n")
        self.assertEqual(lines[0], "1" * (cols - 1) + " NEXT",
                         "EL 0 した行が次の行と切り離されている")


if __name__ == "__main__":
    unittest.main()
