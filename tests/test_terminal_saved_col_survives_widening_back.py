r"""1049 の保存桁が、窓を狭めるたびに不可逆へ丸められる件を検証する。

set_size は代替画面にいるあいだ、メイン画面へ戻る位置 (_saved_main) の
桁を min(saved_col, cols - 1) で書き換えていた。桁を書き換えてしまうと
窓を元へ広げ直しても戻らないので、1049l で戻った位置が本来より左になり、
続く 1 文字が受信済みの桁を上書きして黙って消す。

実測 (基準 f4cad23):
  Screen(3, 8) へ 'ABCDEF' + ESC[?1049h
    saved_main            : (0, 6)
    set_size(3, 4) のあと : (0, 3)   ← 丸められる
    set_size(3, 8) のあと : (0, 3)   ← 広げ直しても戻らない
  -> ESC[?1049l + 'X'
    HEAD : ['ABCXEF', '', '']    (受信した 'D' が消える)
    正   : ['ABCDEFX', '', '']
  同じ形を DECSC / DECRC でやると壊れない (_saved の桁は丸めない)。
  TerminalWidget の文書でも同じ (既定 49 桁 -> 24 桁 -> 49 桁)。
    HEAD : ?1049l + '!' が 24 桁目 (位置 23) を潰す
    正   : '!' は 49 桁目 (位置 48) に載り、受信した桁は残る
  terminal_widget.py の _apply_grid_size は窓・パネル・文字の大きさが
  変わるたびに set_size を呼ぶので、Linux ホストで less や vi を開いた
  まま窓を伸縮する日常操作で起きる。

直し方: set_size での桁の丸めをやめ、保存した桁をそのまま持ち越す。
画面へ収めるのは復元時の _move (max(0, min(cols - 1, col))) が既に
やっている。行の側の min(keep_row, rows - 1) は、履歴へ送った分だけ
減らした keep_row を追っているので、そのまま残す。
狭めたままで戻したときの振る舞い (2026-09-20 の決定) は変わらない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)

DECSC, DECRC = ESC + "7", ESC + "8"
ALT_IN, ALT_OUT = ESC + "[?1049h", ESC + "[?1049l"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def text_of(screen):
    return ["".join(cell[0] for cell in line).rstrip()
            for line in screen.lines]


def ruler(n):
    return "".join(str(i % 10) for i in range(n))


class SavedColumnSurvivesWideningBackTest(unittest.TestCase):
    def test_leaving_the_alternate_screen_after_widening_back(self):
        """狭めて広げ直したあとの 1049l が、受信済みの桁を潰さないこと。"""
        screen = feed(Screen(3, 8), "ABCDEF" + ALT_IN)
        screen.set_size(3, 4)
        screen.set_size(3, 8)
        feed(screen, ALT_OUT + "X")
        self.assertEqual(text_of(screen), ["ABCDEFX", "", ""])

    def test_the_saved_column_is_not_rewritten_by_narrowing(self):
        """狭めても保存桁そのものが書き換わらないこと (丸めは復元時)。"""
        screen = feed(Screen(3, 8), "ABCDEF" + ALT_IN)
        self.assertEqual(screen._saved_main[1], 6)
        screen.set_size(3, 4)
        self.assertEqual(screen._saved_main[1], 6,
                         "狭めた時点で保存桁が丸められている")
        screen.set_size(3, 8)
        self.assertEqual(screen._saved_main[1], 6)

    def test_narrowing_twice_then_widening_back(self):
        """二段階で狭めてから元へ戻しても、桁が戻ること。"""
        screen = feed(Screen(3, 8), "ABCDEF" + ALT_IN)
        screen.set_size(3, 6)
        screen.set_size(3, 4)
        screen.set_size(3, 8)
        feed(screen, ALT_OUT + "X")
        self.assertEqual(text_of(screen), ["ABCDEFX", "", ""])

    def test_the_same_shape_through_decsc_is_unchanged(self):
        """対照: DECSC / DECRC は元から壊れていないこと。"""
        screen = feed(Screen(3, 8), "ABCDEF" + DECSC)
        screen.set_size(3, 4)
        screen.set_size(3, 8)
        feed(screen, DECRC + "X")
        self.assertEqual(text_of(screen), ["ABCDEFX", "", ""])


class NarrowedScreenStillClampsTheRestoredColumnTest(unittest.TestCase):
    """狭めたままのときは、これまでどおり画面の中へ丸めること。"""

    def test_leaving_the_alternate_screen_while_still_narrow(self):
        """狭めたままの 1049l は、これまでどおり右端へ丸めること。"""
        screen = feed(Screen(3, 8), "ABCDEF" + ALT_IN)
        screen.set_size(3, 4)
        feed(screen, ALT_OUT + "X")
        self.assertEqual(text_of(screen), ["ABCXEF", "", ""])

    def test_widening_back_only_partway_still_clamps(self):
        """保存時より狭いところまでしか戻していなければ、丸めること。"""
        screen = feed(Screen(3, 8), "ABCDEF" + ALT_IN)
        screen.set_size(3, 4)
        screen.set_size(3, 6)
        feed(screen, ALT_OUT + "X")
        self.assertEqual(text_of(screen), ["ABCDEX", "", ""])

    def test_the_saved_row_still_follows_the_history(self):
        """行の側は履歴送りを追うので、戻しても上へは帰らないこと。"""
        screen = feed(Screen(3, 8), "a\r\nb\r\nc" + ALT_IN)
        self.assertEqual(screen._saved_main[0], 2)
        screen.set_size(2, 8)       # 1 行を履歴へ送る
        self.assertEqual(screen._saved_main[0], 1)
        screen.set_size(3, 8)
        self.assertEqual(screen._saved_main[0], 1)


class SavedColumnThroughTheWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_every_column_after_widening_back(self):
        """利用者が見る文書でも、戻った 1 文字が受信済みの桁を潰さないこと。"""
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        terminal = widget.create_terminal_tab("dev")
        screen = terminal._screen
        cols = screen.cols
        widget.queue_output("dev", ruler(cols - 1) + ALT_IN)
        widget._flush_pending_output()
        screen.set_size(screen.rows, cols // 2)
        screen.set_size(screen.rows, cols)
        widget.queue_output("dev", ALT_OUT + "!")
        widget._flush_pending_output()
        rows = terminal.toPlainText().split("\n")
        self.assertEqual(rows[0], ruler(cols - 1) + "!",
                         "広げ直したあとの 1 文字が受信済みの桁を潰している")


if __name__ == "__main__":
    unittest.main()
