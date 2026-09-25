"""上へスクロールして過去の出力を読んでいる最中に、窓の幅や文字の大きさを
変えても、見ていた行が上端に残ることを検証する。

何が起きていたか（441ea02 で実測。scratchpad\\v132-triage\\termui\\
repro_ui_scroll_narrow.py / probe_font_scrolled.py と、cx132-termui\\t\\
probe_font_threshold.py）: 100 文字の行を 300 行流し、上端が line 000149 に
なるところまで上へスクロールしてから窓の幅を 1000 から 500 にすると、上端が
line 000049 になった。文字サイズを 10 から 16 にすると line 000029 になった。
Qt は組み直しの前後でスクロールバーの値（ピクセル）をそのまま保つので、
長い行が折り返し直されると上端に別の行が来る。格子の更新（_render_screen）は
描く時点の上端を控えて戻すが、その時点ではもうずれていた。
3000 行を流した端末で文字を大きくすると、さらに悪く、最下部へ飛んで
追従（_follow_output）に変わった（上端 line 002249 → line 002994）。Qt の
組版が遅れて進むあいだ、スクロールバーの最大値が組み終えた分の高さ
（1569）のまま残り、値がそこで切られて「最下部を見ている」と判定される。

どう直したか: InteractiveTerminal の resizeEvent と changeEvent(FontChange)
で、上へスクロールしているときだけ、組み直しの前に上端に見えている表示行
（その先頭の文字の位置と、行内のずれ）を控え、組み直しの後でその文字が
載る表示行の上端へスクロールバーを戻す。スクロールバーの最大値が組版に
追いついていなければ、組版の documentSizeChanged をその場で出して範囲を
合わせてから戻す（Qt が少し後で出すものを先に出すだけ）。最下部を見て
いるとき（追従中）は何もしない。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, "src")


class TerminalScrollKeptOnRelayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _widget(self, lines, line_len=100):
        """line_len 文字の行を lines 行流し、描き切った端末を返す。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.resize(1000, 500)
        w.show()
        terminal = w.create_terminal_tab("dev")
        self._pump(0.1)
        w.append_output("dev", "".join(
            "line %06d %s\r\n" % (i, "x" * (line_len - 12))
            for i in range(lines)))
        end = time.time() + 30
        while w._pending_output and time.time() < end:
            self._pump(0.02)
        self._pump(0.1)
        return w, terminal

    def _scroll_to(self, terminal, number, visual_row=0):
        """number 番目のブロックの visual_row 行目（表示行）を上端にする。"""
        document = terminal.document()
        block = document.findBlockByNumber(number)
        top = document.documentLayout().blockBoundingRect(block).top()
        if visual_row:
            top += block.layout().lineAt(visual_row).y()
        terminal.verticalScrollBar().setValue(int(top))
        self._pump(0.05)
        self.assertFalse(terminal._follow_output, "前提: 上へスクロールしている")

    @staticmethod
    def _top(terminal):
        """上端に見えている表示行の (ブロックの文字列の頭, 行内の位置)。"""
        from PyQt6.QtCore import QPoint
        cursor = terminal.cursorForPosition(QPoint(0, 1))
        return cursor.block().text()[:11], cursor.positionInBlock()

    def _bigger_font(self, w):
        settings = w.current_terminal_settings()
        settings["font_size"] = 16
        w.apply_terminal_settings(settings)

    def test_narrowing_the_window_keeps_the_line_in_view(self):
        """幅を半分にしても、格子の更新の後まで同じ行が上端にあること。"""
        w, terminal = self._widget(300)
        self._scroll_to(terminal, 150)
        before = self._top(terminal)
        self.assertEqual(before[0], "line 000150", "前提: 上端が line 000150")

        w.resize(500, 500)
        self._pump(0.4)   # 格子の更新（200ms のタイマー → _render_screen）まで

        self.assertEqual(self._top(terminal), before,
                         "幅を縮めたら見ていた行がずれた")
        self.assertFalse(terminal._follow_output)

    def test_a_bigger_font_keeps_the_line_in_view(self):
        """文字を大きくしても、同じ行が上端にあること。"""
        w, terminal = self._widget(300)
        self._scroll_to(terminal, 150)
        before = self._top(terminal)

        self._bigger_font(w)
        self._pump(0.4)

        self.assertEqual(self._top(terminal), before,
                         "文字を大きくしたら見ていた行がずれた")
        self.assertFalse(terminal._follow_output)

    def test_a_bigger_font_on_a_long_buffer_does_not_jump_to_the_bottom(self):
        """組版が遅れて進む長さでも、最下部へ飛ばずに同じ行を見せること。"""
        w, terminal = self._widget(3000)
        self._scroll_to(terminal, 2250)
        before = self._top(terminal)

        self._bigger_font(w)
        self._pump(0.4)

        self.assertFalse(terminal._follow_output,
                         "文字を大きくしたら最下部へ飛んで追従に変わった")
        self.assertEqual(self._top(terminal), before,
                         "文字を大きくしたら見ていた行がずれた")

    def test_a_wrapped_line_keeps_the_same_row_in_view(self):
        """折り返した行の途中の表示行を見ていたら、その文字が上端に残ること。

        300 文字の行は 'line 000150 ' のあとで折り返す。2 行目（x の並びの
        始まり）を上端にしてから、縮める・広げるのどちらでも、同じ文字の
        載る表示行が上端に来ること。
        """
        for size in ((500, 500), (1800, 500)):
            with self.subTest(size=size):
                w, terminal = self._widget(300, line_len=300)
                self._scroll_to(terminal, 150, visual_row=1)
                before = self._top(terminal)
                self.assertEqual(before, ("line 000150", 12),
                                 "前提: 折り返した 2 行目が上端")

                w.resize(*size)
                self._pump(0.4)

                self.assertEqual(self._top(terminal), before)

    def test_following_output_stays_at_the_bottom(self):
        """最下部を見ているときは、縮めても文字を大きくしても最下部のままなこと。"""
        w, terminal = self._widget(300)
        bar = terminal.verticalScrollBar()
        self.assertTrue(terminal._follow_output, "前提: 最下部を見ている")

        w.resize(500, 500)
        self._pump(0.4)
        self._bigger_font(w)
        self._pump(0.4)

        self.assertTrue(terminal._follow_output)
        self.assertEqual(bar.value(), bar.maximum())


if __name__ == "__main__":
    unittest.main()
