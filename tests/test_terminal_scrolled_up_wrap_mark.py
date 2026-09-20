r"""上向きのスクロール (SU) と行の削除 (DL) が、スクロール範囲の下端
から上がってきた行の折り返しの印を置き去りにする件を検証する。

5 周目で範囲の上端 (_drop_mark_above)、6 周目で「下端へ上がってきた
行」のうち IL と SD を直したが、鏡の経路が残っていた。SU と DL は範囲の
下端にあった行を上へ動かす。その行の続きは範囲の外 (下端の 1 つ下) に
あって動かないので、印はそこで古くなる。範囲の下端が画面の最下行より
上のとき (DECSTBM で最下行をステータス行として範囲外に残す画面) に、
範囲の中の行と範囲外の行が履歴・コピー・文書で 1 行に繋がる。

実測 (基準 16101ef):
  Screen(4, 4) に ESC[3;1H 'ABCDEFGH' ESC[1;3r ESC[S ESC[3;2H 'Z' で
    text    = ['', 'ABCD', ' Z', 'EFGH']
    wrapped = [False, True, False, False]   <- wrapped[1] が True のまま
  続けて ESC[r ESC[4;1H で履歴へ送ると
    履歴(生) = [('',False), ('',False), ('ABCD',True), (' Z',False)]
    描画側と同じ繋ぎ方 = ['', '', 'ABCD Z']  <- 無関係な 2 本が 1 行に
  SU を DL に置き換えた ESC[3;1H 'ABCDEFGH' ESC[1;3r ESC[1;1H ESC[M
  ESC[3;2H 'Z' も結果は完全に同じ。TerminalWidget を通した文書でも
  2 行目が 'ABCD Z'、3 行目が 'EFGH' になった。範囲を絞らない SU
  (対照) は wrapped が全部 False で正しい。

直し方: 行を動かす前に範囲の下端の印を控え、動いた先 (scroll_bottom -
n) の印を外す。最下行での折り返し (_linefeed(from_wrap=True)) の印だけ
はそこに載っているので、その経路だけ _scroll_up(from_wrap=True) で
除外する。動いた先が範囲の外へ出た (押し出されて履歴へ入った) ときは
触らないので、履歴の記録はこれまでどおり。
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


def joined_history(screen):
    """描画側と同じ繋ぎ方 (印のある行は次と繋ぐ) で履歴を組み直す。"""
    joined, buf = [], ""
    for line, wrapped in screen.take_new_history():
        buf += "".join(cell[0] for cell in line).rstrip()
        if not wrapped:
            joined.append(buf)
            buf = ""
    if buf:
        joined.append(buf)
    return joined


def region_sample(move):
    """範囲の下端に折り返しの続きがある画面を作り、move を流す。

    4 行目は範囲の外。2 行目から 3 行目へ折り返した行の続きが 4 行目に
    あるので、3 行目が範囲の下端として上がると印が古くなる。
    """
    return feed(Screen(4, 4), ESC + "[3;1H" + "ABCDEFGH" + ESC + "[1;3r"
                + move + ESC + "[3;2H" + "Z")


SCROLL_UP = ESC + "[S"
DELETE_LINE = ESC + "[1;1H" + ESC + "[M"


class ScrolledUpWrapMarkTest(unittest.TestCase):
    def test_the_sample_screen_starts_wrapped(self):
        """出発点の画面が、範囲の下端で折り返していること。"""
        s = feed(Screen(4, 4), ESC + "[3;1H" + "ABCDEFGH")
        self.assertEqual(s.text(), ["", "", "ABCD", "EFGH"])
        self.assertEqual(s.wrapped, [False, False, True, False])

    def test_scroll_up_drops_the_mark_of_the_row_that_moved_up(self):
        """SU で上がった行の印が外れること。"""
        s = region_sample(SCROLL_UP)
        self.assertEqual(s.text(), ["", "ABCD", " Z", "EFGH"])
        self.assertFalse(s.wrapped[1],
                         "続きが範囲の外に残ったまま印が付いている")

    def test_delete_line_drops_the_mark_of_the_row_that_moved_up(self):
        """DL で上がった行の印も外れること。"""
        s = region_sample(DELETE_LINE)
        self.assertEqual(s.text(), ["", "ABCD", " Z", "EFGH"])
        self.assertFalse(s.wrapped[1],
                         "続きが範囲の外に残ったまま印が付いている")

    def test_scroll_up_does_not_join_two_logical_lines_in_history(self):
        """SU のあと、履歴で無関係な 2 本が 1 行に繋がらないこと。"""
        s = region_sample(SCROLL_UP)
        feed(s, ESC + "[r" + ESC + "[4;1H" + "\n\n\n")
        self.assertEqual(joined_history(s), ["", "", "ABCD", " Z"])

    def test_delete_line_does_not_join_two_logical_lines_in_history(self):
        """DL のあとも、履歴で 1 行に繋がらないこと。"""
        s = region_sample(DELETE_LINE)
        feed(s, ESC + "[r" + ESC + "[4;1H" + "\n\n\n")
        self.assertEqual(joined_history(s), ["", "", "ABCD", " Z"])

    def test_the_alternate_screen_drops_the_mark_without_history(self):
        """代替画面でも印は外れ、履歴は増えないこと。"""
        s = feed(Screen(4, 4), ESC + "[?1049h" + ESC + "[3;1H" + "ABCDEFGH"
                 + ESC + "[1;3r" + ESC + "[S")
        self.assertTrue(s.alt_active)
        self.assertFalse(s.wrapped[1], "続きを範囲の外へ置いた印が残る")
        self.assertEqual(history_rows(s), [])


class ScrolledUpWrapMarkKeepsWorkingTest(unittest.TestCase):
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
        """範囲の下端で折り返して範囲が上がっても、印が残ること。"""
        s = feed(Screen(4, 4), ESC + "[1;3r" + ESC + "[3;1H" + "ABCDX")
        self.assertEqual(s.text(), ["", "ABCD", "X", ""])
        self.assertTrue(s.wrapped[1], "範囲内の折り返しの印が消えている")

    def test_a_device_newline_at_the_region_bottom_keeps_scrolling(self):
        """機器が送った改行での巻き上げが、これまでどおりであること。"""
        s = feed(Screen(4, 4), ESC + "[1;3r" + ESC + "[3;1H" + "ZZ\r\n" + "Y")
        self.assertEqual(s.text(), ["", "ZZ", "Y", ""])
        self.assertEqual(s.wrapped, [False, False, False, False])

    def test_a_full_screen_region_still_records_history(self):
        """範囲を狭めない SU の履歴が、印ごとこれまでどおりであること。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[S")
        self.assertEqual(history_rows(s), [("ABCD", True)])

    def test_scrolling_the_whole_region_away_keeps_the_history_mark(self):
        """範囲ごと押し出された行は、印を付けたまま履歴へ入ること。"""
        s = feed(Screen(4, 4), "ABCDX" + ESC + "[4S")
        self.assertEqual(s.text(), ["", "", "", ""])
        self.assertEqual(history_rows(s),
                         [("ABCD", True), ("X", False), ("", False),
                          ("", False)])

    def test_insert_line_and_scroll_down_are_unchanged(self):
        """下向き (IL / SD) の扱いは 6 周目のままであること。"""
        for move in (ESC + "[1;1H" + ESC + "[L", ESC + "[T"):
            s = feed(Screen(4, 4), ESC + "[1;%dr" % 3 + ESC + "[4;1H"
                     + "STAT" + ESC + "[2;1H" + "ABCDEFGH" + move)
            self.assertEqual(s.text(), ["", "", "ABCD", "STAT"])
            self.assertFalse(s.wrapped[2], "下端へ上がった行の印が残る")


class ScrolledUpWrapMarkThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def document_after(self, move):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        terminal._screen = Screen(4, 4)
        w.queue_output("dev", ESC + "[3;1H" + "ABCDEFGH" + ESC + "[1;3r"
                       + move + ESC + "[3;2H" + "Z"
                       + ESC + "[r" + ESC + "[4;1H" + "\n\n\n")
        w._flush_pending_output()
        return terminal.toPlainText().split("\n")

    def test_the_document_keeps_the_two_lines_apart_after_scroll_up(self):
        """SU のあと、文書で 2 本の論理行が 1 行にならないこと。"""
        self.assertNotIn("ABCD Z", self.document_after(SCROLL_UP),
                         "無関係な 2 行が 1 行に繋がっている")

    def test_the_document_keeps_the_two_lines_apart_after_delete_line(self):
        """DL のあとも、文書で 1 行にならないこと。"""
        self.assertNotIn("ABCD Z", self.document_after(DELETE_LINE),
                         "無関係な 2 行が 1 行に繋がっている")


if __name__ == "__main__":
    unittest.main()
