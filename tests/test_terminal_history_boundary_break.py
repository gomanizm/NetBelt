r"""履歴と画面の境をまたぐ長い行の 0 行目が別の行に入れ替わっても、過去の行へ繋がる件を検証する。

履歴へ送った最後の行は折り返しの印ごと記録され、描画側はそれを改行なしで
文書へ書く (画面の 0 行目と同じブロックになる)。そのあと 0 行目が別の行に
入れ替わっても、その繋がりを切る口が無かった。画面の中では、行の下へ別の
行が来たら上の行の印を外している (_drop_mark_above、行頭からの印字) が、
0 行目の上 (= 履歴の最後の行) には届かない。

実測 (基準 441ea02 = v1.3.1):
  Screen(2, 4) へ 'ABCDEFGHI' (ABCD は折り返しの印付きで履歴へ) のあと
    ESC[H ESC[L 'Z'  (IL)       HEAD : 論理行 'ABCDZ\nEFGH'
    ESC[H ESC M 'Z'  (上端の RI) HEAD : 'ABCDZ\nEFGH'
    ESC[H ESC[T 'Z'  (SD)       HEAD : 'ABCDZ\nEFGH'
    ESC[H 'Z'        (原点から)  HEAD : 'ABCDZFGH\nI'
  TerminalWidget (3x10) へ a*10 b*10 c*10 d*5 のあと ESC[H ESC[L 'inserted'
    HEAD : unwrapped_text の 1 行目 'aaaaaaaaaainserted'
  そのあと 0 行目を履歴へ押し出しても繋がったまま残る (全ログ保存に入る)。

直し方: Screen が「履歴の最後の行は 0 行目へ続いている」(_history_open) を
覚え、0 行目へ別の行が来たとき (0 行目での IL、上端が画面の先頭の SD と
RI、0 行目の行頭から折り返しでなく書き始めたとき) に閉じる。描画側へまだ
渡していなければ、差分の最後の行の印を外す。渡し済みなら描画側へ知らせ
(take_history_break)、描画側が文書の境目へ改行を 1 つ入れる。境目が既に
行頭 (MAX_BLOCK_CHARS で切ってある) なら入れない。
代替画面にいる間は閉じない (利用者の決定 (a)。退場すれば 0 行目は元の
続きへ戻るので、閉じると本物の続きを割る)。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
CSI = ESC + "["
NL = chr(10)
CRLF = chr(13) + chr(10)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def logical(screen):
    """描画側と同じ規則で、履歴の差分と画面を 1 本の文字列にする。"""
    out = []
    for line, wrapped in screen.take_new_history():
        t = "".join(c[0] for c in line)
        out.append(t if wrapped else t.rstrip() + NL)
    last = screen.rows - 1
    for r, line in enumerate(screen.lines):
        t = "".join(c[0] for c in line)
        out.append(t if screen.wrapped[r] and r < last else t.rstrip() + NL)
    return "".join(out)


class HistoryBoundaryTest(unittest.TestCase):
    CASES = (("IL", CSI + "H" + CSI + "LZ", "ABCD\nZ\nEFGH\n"),
             ("RI", CSI + "H" + ESC + "MZ", "ABCD\nZ\nEFGH\n"),
             ("SD", CSI + "H" + CSI + "TZ", "ABCD\nZ\nEFGH\n"),
             ("home", CSI + "HZ", "ABCD\nZFGH\nI\n"))

    def test_replacing_row_zero_ends_the_history_line(self):
        """IL・上端の RI・SD・原点からの印字で、0 行目を過去の行へ繋げないこと。"""
        for name, seq, want in self.CASES:
            with self.subTest(name):
                screen = feed(Screen(2, 4), "ABCDEFGHI" + seq)
                self.assertEqual(logical(screen), want)

    def test_a_history_line_already_handed_over_is_closed_by_the_renderer(self):
        """描画側へ渡し済みの履歴の行は、描画側へ閉じるよう知らせること (1 回だけ)。"""
        for name, seq, _ in self.CASES:
            with self.subTest(name):
                screen = feed(Screen(2, 4), "ABCDEFGHI")
                self.assertEqual(screen.take_new_history()[-1][1], True,
                                 "前提: 履歴の最後の行は 0 行目へ続いている")
                self.assertFalse(screen.take_history_break())
                feed(screen, seq)
                self.assertTrue(screen.take_history_break())
                self.assertFalse(screen.take_history_break())

    def test_a_plain_wrap_still_joins(self):
        """対照: 普通の折り返し (最下行・1 行の画面) は 1 行のまま繋がること。"""
        self.assertEqual(logical(feed(Screen(2, 4), "ABCDEFGHIJKL")),
                         "ABCDEFGHIJKL\n")
        self.assertEqual(logical(feed(Screen(1, 4), "ABCDEFGHIJ")),
                         "ABCDEFGHIJ\n")

    def test_writing_into_row_zero_past_the_start_still_joins(self):
        """対照: 0 行目の行頭以外への書き直しは、履歴の行と繋がったままのこと。

        右端まで届かない書き直しなので、0 行目から 1 行目への印は画面の中の
        規則どおり外れる (ここは変えない)。
        """
        screen = feed(Screen(2, 4), "ABCDEFGHI" + CSI + "1;2Hx")
        self.assertEqual(logical(screen), "ABCDExGH\nI\n")

    def test_nothing_is_closed_when_the_history_line_did_not_continue(self):
        """対照: 履歴の最後の行が続いていなければ、何も知らせないこと。"""
        screen = feed(Screen(2, 4), "AB" + CRLF + "CD" + CRLF + "EF")
        screen.take_new_history()
        feed(screen, CSI + "H" + CSI + "LZ")
        self.assertFalse(screen.take_history_break())

    def test_the_alternate_screen_does_not_close_the_line(self):
        """代替画面の中の IL・印字では閉じず、戻ったあとも続きのまま繋がること。"""
        screen = feed(Screen(2, 4), "ABCDEFGHI" + CSI + "?1049h"
                      + CSI + "H" + CSI + "LZ" + CSI + "Hvi" + CSI + "?1049l")
        self.assertFalse(screen.take_history_break())
        self.assertEqual(logical(screen), "ABCDEFGHI\n")


class HistoryBoundaryThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self, rows, cols):
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        terminal = widget.create_terminal_tab("dev")
        terminal._screen.set_size(rows, cols)
        widget._render_screen(terminal)
        return widget, terminal

    def test_an_inserted_line_is_not_glued_to_the_history(self):
        """文書 (コピー・全ログ保存の元) で、挿入した行が過去の行へ繋がらないこと。"""
        for name, seq, want in (
                ("IL", CSI + "H" + CSI + "Linserted", "inserted"),
                ("RI", CSI + "H" + ESC + "Mrevealed", "revealed"),
                ("SD", CSI + "H" + CSI + "Tscrolled", "scrolled")):
            with self.subTest(name):
                widget, terminal = self._terminal(3, 10)
                widget.append_output(
                    "dev", "a" * 10 + "b" * 10 + "c" * 10 + "d" * 5)
                widget.append_output("dev", seq)
                widget.append_output("dev", CSI + "3;1H" + CRLF * 3 + "end")
                lines = terminal.unwrapped_text().split(NL)
                self.assertEqual(lines[:3],
                                 ["a" * 10, want, "b" * 10 + "c" * 10])

    def test_the_same_in_one_piece_of_output(self):
        """押し出しと入れ替えが同じ受信片に入っていても、繋がらないこと。"""
        widget, terminal = self._terminal(3, 10)
        widget.append_output("dev", "a" * 10 + "b" * 10 + "c" * 10 + "d" * 5
                             + CSI + "H" + CSI + "Linserted")
        lines = terminal.unwrapped_text().split(NL)
        self.assertEqual(lines[:3], ["a" * 10, "inserted", "b" * 10 + "c" * 10])

    def test_a_line_already_cut_at_the_block_limit_gets_no_empty_line(self):
        """長い行が MAX_BLOCK_CHARS で切れた直後に閉じても、空行を足さないこと。"""
        widget, terminal = self._terminal(3, 100)
        # 85 行ぶん。82 行 (8200 文字) が履歴へ出て、そこでブロックが切れる
        widget.append_output("dev", "x" * 8500)
        self.assertEqual(terminal._region.positionInBlock(), 0,
                         "前提: 履歴の最後の行のあとでブロックが切れている")
        widget.append_output("dev", CSI + "Hhome")
        lines = terminal.unwrapped_text().split(NL)
        self.assertEqual([len(x) for x in lines], [8200, 100, 200])
        self.assertTrue(lines[1].startswith("home"))


if __name__ == "__main__":
    unittest.main()
