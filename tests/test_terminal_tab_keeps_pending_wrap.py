r"""右端で折り返し待ちのときの TAB が、右端の受信済みの文字を潰させる件を検証する。

_ctrl の TAB の枝は、右端で折り返し待ちのときも待ちを解いていた。
カーソルは min(桁 - 1, 次のタブ位置) で右端のままなので、続く 1 文字は
次の行へ行かず、右端の受信済みの文字を上書きして黙って消した。

実測 (基準 441ea02 = v1.3.1):
  Screen(3, 4) へ 'ABCD' TAB 'X'
    HEAD : 0 行目 ['A', 'B', 'C', 'X']、カーソル (0, 3)   (D が消える)
    正   : ['ABCD', 'X', '']、0 行目は折り返しで次の行へ続く
  TerminalWidget の文書 (20 桁) へ '0123456789abcdefghij' TAB 'tail'
    HEAD : unwrapped_text の 1 行目 '0123456789abcdefghitail' (j が消える)
    正   : '0123456789abcdefghijtail'
  コピーと全ログ保存でも欠ける (ログ記録は受信した事象を書くので残る)。
  xterm の TAB (TabToNextStop) は桁を動かすだけで ResetWrap しないので、
  折り返し待ちが残り、X は次の行へ行って D は残る。最後に書いたセルの
  覚え (char_was_written) も落とさないので、TAB のあとの結合文字は D に付く。

直し方: 右端で折り返し待ちのときの TAB は何もしない (桁を動かさず、待ちと
最終桁へ印字した覚えを保つ)。待ちが立っていなければ今までどおり。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
TAB = chr(9)
NL = chr(10)
AC = chr(0x0301)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def rows(screen):
    return ["".join(c[0] for c in line).rstrip() for line in screen.lines]


class TabAtThePendingWrapTest(unittest.TestCase):
    def test_the_next_character_goes_to_the_next_row(self):
        """'ABCD' TAB 'X' で D が残り、X は次の行へ行くこと (xterm と同じ)。"""
        screen = feed(Screen(3, 4), "ABCD" + TAB + "X")
        self.assertEqual(rows(screen), ["ABCD", "X", ""])
        self.assertTrue(screen.wrapped[0])

    def test_several_tabs_do_not_undo_the_wait(self):
        """TAB を重ねても待ちは残り、D は消えないこと。"""
        screen = feed(Screen(3, 4), "ABCD" + TAB + TAB + "X")
        self.assertEqual(rows(screen), ["ABCD", "X", ""])

    def test_a_combining_mark_after_the_tab_joins_the_last_character(self):
        """TAB のあとの結合文字は、右端に書いた D に付くこと (xterm と同じ)。"""
        screen = feed(Screen(3, 4), "ABCD" + TAB + AC)
        self.assertEqual([c[0] for c in screen.lines[0]],
                         ["A", "B", "C", "D" + AC])

    def test_a_tab_before_the_edge_still_moves(self):
        """対照: 右端より手前の TAB は、これまでどおり次のタブ位置へ進むこと。"""
        screen = feed(Screen(3, 20), "AB" + TAB + "X")
        self.assertEqual(rows(screen)[0], "AB      X")

    def test_a_tab_that_stops_at_the_edge_still_overwrites(self):
        """対照: 待ちの無い位置から右端で止まった TAB のあとは、右端へ書くこと。"""
        screen = feed(Screen(3, 10), "ABCDEFGHI" + TAB + "X")
        self.assertEqual(rows(screen), ["ABCDEFGHIX", "", ""])

    def test_the_tab_without_autowrap_is_unchanged(self):
        """対照: 折り返し無効 (待ちが立たない) の右端の TAB も今までどおり。"""
        screen = feed(Screen(3, 4), ESC + "[?7lABCD" + TAB + "X")
        self.assertEqual(rows(screen), ["ABCX", "", ""])


class TabAtThePendingWrapThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_last_column(self):
        """利用者が見る文書 (コピー・全ログ保存の元) でも、右端の文字が消えないこと。"""
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        terminal = widget.create_terminal_tab("dev")
        terminal._screen.set_size(5, 20)
        widget._render_screen(terminal)
        widget.append_output("dev", "0123456789abcdefghij" + TAB + "tail")
        first = terminal.unwrapped_text().split(NL)[0]
        self.assertEqual(first, "0123456789abcdefghijtail")


if __name__ == "__main__":
    unittest.main()
