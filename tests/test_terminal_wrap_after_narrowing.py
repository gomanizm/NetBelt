"""窓を狭めたあとの折り返しに、古い文字や空白が混ざらないことを検証する。

画面モデルの前提は「行の長さが、そのままどこで折り返したかを表す」で、
描画側もそれに乗っている (terminal_widget の履歴描画は、折り返し行だけ
末尾を刈らずに次の行へ繋げる)。

ところが桁を狭めるとき行は切り詰められない。これは v1.2.0 で意図的に
選ばれた設計で、リサイズのたびに全行を組み直すと往復のたびに内容が
削れる回帰があったため、「書かれた行には触らない」ことにした結果である。

その副作用として、狭めた直後の行は旧桁数ぶんのセルを持ったまま残る。
そこへ新しい出力が来て今の桁で折り返すと、その行が折り返し行として
印を付けられ、「行の長さ = 折り返し位置」という前提が崩れる。描画側は
その行のセルを丸ごと次の行へ繋げるので、狭める前にその行にあった古い
文字（または空白の塊）が 1 行の途中へ差し込まれる。

利用者から見ると、スクロールバックのその行をコピーすると折り返し位置に
覚えのない文字列が挟まっている、という形になる。

直し方として行を組み直す (reflow) 方向へは戻さない。折り返しの印を
付ける時点で、その行の論理的な内容はちょうど今の桁幅ぶんなので、
そこで行の長さを揃えれば前提が回復する。機器が送った改行では
切り詰めない（広い窓で書かれた長い行は、そのまま残す）。
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


class WrapWidthOnTheModelTest(unittest.TestCase):
    def _narrowed_then_wrapped(self):
        """80 桁で書いた行の上に、40 桁で折り返す出力を重ねる。"""
        s = Screen(rows=24, cols=80)
        feed(s, "A" * 80 + "\r")        # 行に 80 セル残し、桁だけ先頭へ戻す
        s.set_size(24, 40)
        feed(s, "B" * 50)               # 40 桁で折り返す
        return s

    def test_a_wrapped_line_is_as_long_as_the_wrap(self):
        """折り返し行の長さが、折り返した桁と一致すること。"""
        s = self._narrowed_then_wrapped()
        self.assertTrue(s.wrapped[0], "折り返しの印が付いていない")
        self.assertEqual(len(s.lines[0]), 40,
                         "折り返し行が旧桁数のまま残っている")

    def test_the_stale_tail_is_not_part_of_the_wrapped_line(self):
        """折り返し行に、狭める前の文字が残っていないこと。"""
        s = self._narrowed_then_wrapped()
        row = "".join(cell[0] for cell in s.lines[0])
        self.assertNotIn("A", row,
                         "狭める前の文字が折り返し行に残っている")

    def test_a_long_line_that_is_not_rewritten_keeps_its_length(self):
        """書き直されていない長い行は、狭めても短くしないこと。

        v1.2.0 で reflow をやめた理由がこれ。触ると往復のたびに削れる。
        """
        s = Screen(rows=24, cols=80)
        feed(s, "A" * 80)
        s.set_size(24, 40)
        self.assertEqual(len(s.lines[0]), 80,
                         "触っていない行まで切り詰めている")

    def test_a_line_ended_by_the_device_keeps_its_length(self):
        """機器が改行を送って終えた行は、狭めても切り詰めないこと。"""
        s = Screen(rows=24, cols=80)
        feed(s, "A" * 80 + "\r")
        s.set_size(24, 40)
        feed(s, "\n")                   # 折り返しではない改行
        self.assertFalse(s.wrapped[0], "改行が折り返し扱いになっている")
        self.assertEqual(len(s.lines[0]), 80,
                         "機器が終えた行を切り詰めている")


class WrapWidthOnTheWidgetTest(unittest.TestCase):
    """利用者が実際に見る文書の側で確かめる。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_output_after_narrowing_reads_as_one_line_in_the_scrollback(self):
        """狭めた直後に流れた出力が、記録では1続きで読めること。

        画面に出ている間は、折り返し行が2行を占めるのが正しい（実端末と
        同じ）。問題になるのは記録の側で、そちらは折り返しを繋いで
        1行として残す。報告も「スクロールバックのその行をコピーすると
        覚えのない文字列が挟まっている」という形だった。
        """
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w._grid_size = lambda t: (24, 80)
        w.create_terminal_tab("dev")
        w._apply_grid_size("dev")

        w.append_output("dev", "A" * 80 + "\r")
        w._grid_size = lambda t: (24, 40)
        w._apply_grid_size("dev")
        w.append_output("dev", "B" * 50)
        # 折り返した行を画面から押し出して、記録へ送る
        w.append_output("dev", "\r\n" * 30)

        text = w._terminals["dev"].toPlainText()
        self.assertNotIn("A", text,
                         "狭める前の文字が記録に残っている")
        self.assertIn("B" * 50, text,
                      "折り返し位置で出力が途切れている（古い文字か空白が挟まった）")


if __name__ == "__main__":
    unittest.main()
