r"""端末の大きさを変えると、折り返し待ちの 1 文字が黙って消える件を検証する。

右端ちょうどまで書いて出力が止まっている間、画面は「折り返し待ち」
(_pending_wrap) でカーソルを右端に置いたまま次の文字を待つ。ここへ
set_size が来ると、基準 16101ef では _pending_wrap を無条件に False へ
落としていたため、続きの 1 文字目が右端の文字を上書きして、その文字が
画面からも折り返しの印からも消えていた。

実測 (基準 16101ef):
  Screen(2, 4) に 'ABCD' -> text=['ABCD','']  cursor=(0,3) _pending_wrap=True
  set_size(3, 4)         -> _pending_wrap=False (cursor は (0,3) のまま)
  'X'                    -> text=['ABCX','','']  wrapped=[False,False,False]
  リサイズなしの対照 Screen(3,4)+'ABCD'+'X' は text=['ABCD','X','']
  wrapped=[True,False,False]。'D' が画面からも印からも消える。

行数だけの変更でも桁の変更でも起きる (Screen(24,80) に '-'*80 を書いて
から 'continued'):
  リサイズなし     : 0 行目 '-'x80、1 行目 'continued'
  set_size(23, 80) : 0 行目 '-'x79+'c'、1 行目 'ontinued'
  set_size(24, 100): 0 行目 '-'x79+'continued'
TerminalWidget を通した文書 (コピーと全ログ保存の元) でも 0 行目の '-' は
79 個だった。set_size を呼ぶのは窓のリサイズと文字サイズの変更なので、
どちらも日常操作で起きる。

直し方: 折り返し待ちを「カーソルは論理的に桁 cols にいる」状態として
持ち越す。桁で丸める前に logical_col = cursor_col + (1 if 折り返し待ち
else 0) を取り、cursor_col = min(logical_col, cols - 1)、折り返し待ちは
logical_col >= 新しい cols のときだけ残す。桁が広がったときは折り返し
待ちを解いて、カーソルを旧桁の位置へ進める ('ABCD' のあとの 'X' が
5 桁目に入る)。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class ResizePendingWrapTest(unittest.TestCase):
    def test_the_pending_wrap_survives_a_row_count_change(self):
        """行数だけ変えても、続きの 1 文字が右端を上書きしないこと。"""
        s = feed(Screen(2, 4), "ABCD")
        self.assertTrue(s._pending_wrap, "前提: 折り返し待ちになっていない")
        s.set_size(3, 4)
        feed(s, "X")
        self.assertEqual(s.text(), ["ABCD", "X", ""])
        self.assertTrue(s.wrapped[0], "折り返しの印が立っていない")

    def test_the_control_without_a_resize_looks_the_same(self):
        """リサイズを挟まない対照が、同じ画面になること。"""
        s = feed(Screen(3, 4), "ABCDX")
        self.assertEqual(s.text(), ["ABCD", "X", ""])
        self.assertEqual(s.wrapped, [True, False, False])

    def test_a_full_width_line_keeps_every_column_across_a_resize(self):
        """80 桁ちょうどの行が、行数の変更で 1 文字も欠けないこと。"""
        s = feed(Screen(24, 80), "-" * 80)
        s.set_size(23, 80)
        feed(s, "continued")
        self.assertEqual(s.text()[0], "-" * 80)
        self.assertEqual(s.text()[1], "continued")

    def test_widening_moves_the_cursor_past_the_old_right_edge(self):
        """桁が広がったら折り返し待ちを解き、旧桁の位置から続けること。"""
        s = feed(Screen(24, 80), "-" * 80)
        s.set_size(24, 100)
        self.assertFalse(s._pending_wrap, "桁が広がったのに折り返し待ちが残る")
        feed(s, "continued")
        self.assertEqual(s.text()[0], "-" * 80 + "continued")

    def test_widening_a_tiny_screen_keeps_the_last_column(self):
        """狭い画面を広げても、右端の文字が上書きされないこと。"""
        s = feed(Screen(2, 4), "ABCD")
        s.set_size(2, 6)
        feed(s, "X")
        self.assertEqual(s.text()[0], "ABCDX")

    def test_narrowing_keeps_the_pending_wrap(self):
        """桁が狭くなったときは、折り返し待ちのまま次の行へ送ること。"""
        s = feed(Screen(24, 80), "-" * 80)
        s.set_size(24, 60)
        self.assertTrue(s._pending_wrap, "折り返し待ちが落ちている")
        feed(s, "continued")
        self.assertEqual(s.text()[1], "continued")

    def test_a_resize_without_a_pending_wrap_still_clamps_the_cursor(self):
        """折り返し待ちでないときの桁の丸めは、これまでどおりであること。"""
        s = feed(Screen(4, 8), "ABCDEF")
        self.assertFalse(s._pending_wrap)
        s.set_size(4, 4)
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 3))
        self.assertFalse(s._pending_wrap)


class ResizePendingWrapThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_last_column_after_a_resize(self):
        """利用者が見る文書でも、右端の 1 文字が消えないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        screen = terminal._screen
        cols = screen.cols
        w.queue_output("dev", "-" * cols)
        w._flush_pending_output()
        screen.set_size(screen.rows - 1, cols)
        w.queue_output("dev", "continued\r\n")
        w._flush_pending_output()
        self.assertEqual(terminal.toPlainText().split("\n")[0].count("-"),
                         cols, "リサイズで行末の文字が消えている")


if __name__ == "__main__":
    unittest.main()
