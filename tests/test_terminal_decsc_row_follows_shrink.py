r"""ESC 7 で控えた行が、窓を縦に縮めたときの履歴送りに追随する件を検証する。

set_size は縦に縮めるとき、メイン画面の上の行を履歴へ送り、送った数だけ
残す行 (keep_row) を上へずらす。生きているカーソルと、代替画面に居る間の
1049 の保存 (_saved_main) はこの keep_row で追っているが、DECSC の保存領域
(ESC 7 / ?1048h。メイン画面にいれば _saved、代替画面にいる間は
_other_saved) の行は据え置いていた。そのため ESC 8 が控えた行より下の、
受信済みの別の行へ戻って上書きする (復元時の _move が rows - 1 へ丸める
ので、画面の外へは出ない)。

実測 (基準 441ea02 = v1.3.1):
  Screen(4, 8) へ 'AAAA' CRLF 'BBBB' CRLF 'CCCC' CRLF 'DDDD'
  → ESC[3;2H ESC 7 → set_size(2, 8) → ESC 8 '!'
    HEAD : ['CCCC', 'D!DD']   (受信済みの 'D' を潰す)
    正   : ['C!CC', 'DDDD']
  ?1048h / ?1048l、ESC 7 のあと 47h / 1047h / 1049h で代替画面に居る間に
  縮めた場合、47h から 1049l で出た場合 (DECSC から戻す) も同じ。
  縮めてから元の高さへ戻すと、ESC 8 は空行へ戻る ([.., ' !', ''])。
  TerminalWidget でも同じ (窓の高さ 25 行 → 14 行で、下から 2 行目に
  控えた ESC 8 '!' が最下行の受信済みの桁を潰した)。
  生きているカーソルと、代替画面に居る間の 1049 の保存は同じ形で正しく
  追随する (先にメイン画面へ戻ったあとの 1049 の保存は下の追記)。

起きるのは ESC 7 と ESC 8 の間 (別の受信片) に窓を縦に縮めたときだけ。

直し方: set_size で履歴へ送った行数を数え、メイン画面の DECSC の保存
(alt_active なら _other_saved、そうでなければ _saved) の行から引く
(0 で止める)。代替画面の保存は代替画面が下を切るだけなので触らない。

追記 (1049 の保存が 47l / 1047l のあとに残っている場合):
1049h のあと 47l / 1047l で先にメイン画面へ戻ると、1049h の保存
(_saved_main) はメイン画面に居る間も残り、あとの 1049l がそこから戻す
(_switch_screen の早期 return)。set_size が _saved_main の行を keep_row で
追うのは alt_active のときだけだったので、この間に縮めると行が据え置きに
なり、1049l が控えた行より下の受信済みの行へ戻って潰していた。

実測 (基準 ebbe593。441ea02 = v1.3.1 も同じ):
  上と同じ 4 行 → ESC[3;2H ESC[?1049h ESC[?47l ESC[4;1H → set_size(2, 8)
  → ESC[?1049l '!'
    ebbe593 : ['CCCC', 'D!DD']   (受信済みの 'D' を潰す)
    正      : ['C!CC', 'DDDD']
  ?1047l でも同じ。縮めてから元の高さへ戻すと、1049l は空行へ戻る
  (['CCCC', 'DDDD', ' !', ''])。

直し方: メイン画面に居るとき (alt_active でないとき) は、履歴へ送った
行数を _saved_main の行からも引く (0 で止める)。桁は触らない。
引くのは履歴へ送った行数だけで、下の空行を捨てた分は引かない。減った
行の合計で引くと、控えた行より上の受信済みの行を潰す (検査で試した
変異: 'AAAA' CRLF 'BBBB' ESC[2;2H ESC[?1049h ESC[?47l ESC[2;1H →
set_size(2, 8) → ESC[?1049l '!' が ['A!AA', 'BBBB'] になる。正は
['AAAA', 'B!BB'])。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
CRLF = chr(13) + chr(10)
DECSC, DECRC = ESC + "7", ESC + "8"
FILL4 = CRLF.join(["AAAA", "BBBB", "CCCC", "DDDD"])


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def text_of(screen):
    return ["".join(cell[0] for cell in line).rstrip()
            for line in screen.lines]


class SavedRowFollowsTheHistoryTest(unittest.TestCase):
    """縮めて上の行を履歴へ送ったら、控えた行も同じだけ上へ。"""

    def test_esc8_after_shrinking_returns_to_the_saved_line(self):
        """ESC 7 / ESC 8 の間に縮めても、控えた行へ戻ること。"""
        screen = feed(Screen(4, 8), FILL4 + ESC + "[3;2H" + DECSC)
        screen.set_size(2, 8)
        feed(screen, DECRC + "!")
        self.assertEqual(text_of(screen), ["C!CC", "DDDD"])

    def test_1048_after_shrinking_returns_to_the_saved_line(self):
        """?1048h / ?1048l も同じ保存領域なので、控えた行へ戻ること。"""
        screen = feed(Screen(4, 8), FILL4 + ESC + "[3;2H" + ESC + "[?1048h")
        screen.set_size(2, 8)
        feed(screen, ESC + "[?1048l" + "!")
        self.assertEqual(text_of(screen), ["C!CC", "DDDD"])

    def test_a_save_parked_behind_47_follows_the_shrink(self):
        """代替画面 (47) に居る間に縮めても、メイン画面の保存が追うこと。"""
        screen = feed(Screen(4, 8), FILL4 + ESC + "[3;2H" + DECSC
                      + ESC + "[?47h")
        screen.set_size(2, 8)
        feed(screen, ESC + "[?47l" + DECRC + "!")
        self.assertEqual(text_of(screen), ["C!CC", "DDDD"])

    def test_a_save_parked_behind_1049_follows_the_shrink(self):
        """1049 の間も、裏の DECSC の保存が 1049 の保存と同じく追うこと。"""
        screen = feed(Screen(4, 8), FILL4 + ESC + "[3;2H" + DECSC
                      + ESC + "[4;3H" + ESC + "[?1049h")
        screen.set_size(2, 8)
        feed(screen, ESC + "[?1049l" + DECRC + "!")
        self.assertEqual(text_of(screen), ["C!CC", "DDDD"])

    def test_leaving_47_with_1049l_restores_the_shifted_save(self):
        """47h から 1049l で出ると DECSC から戻す。その行も追っていること。"""
        screen = feed(Screen(4, 8), FILL4 + ESC + "[3;2H" + DECSC
                      + ESC + "[?47h")
        screen.set_size(2, 8)
        feed(screen, ESC + "[?1049l" + "!")
        self.assertEqual(text_of(screen), ["C!CC", "DDDD"])

    def test_shrinking_in_two_steps_moves_the_save_twice(self):
        """二段階で縮めても、送った行の合計だけ上へずれること。"""
        screen = feed(Screen(5, 8),
                      CRLF.join(["AAAA", "BBBB", "CCCC", "DDDD", "EEEE"])
                      + ESC + "[4;2H" + DECSC + ESC + "[5;1H")
        screen.set_size(4, 8)
        screen.set_size(3, 8)
        feed(screen, DECRC + "!")
        self.assertEqual(text_of(screen), ["CCCC", "D!DD", "EEEE"])

    def test_growing_back_does_not_land_on_a_blank_row(self):
        """縮めて元の高さへ戻したあとの ESC 8 が、下に足した空行へ行かないこと。"""
        screen = feed(Screen(4, 8), FILL4 + ESC + "[3;2H" + DECSC)
        screen.set_size(2, 8)
        screen.set_size(4, 8)
        feed(screen, DECRC + "!")
        self.assertEqual(text_of(screen), ["C!CC", "DDDD", "", ""])


class A1049SaveLeftOnTheMainScreenTest(unittest.TestCase):
    """1049h のあと 47l / 1047l で先にメイン画面へ戻り、残った 1049 の保存。"""

    LEAVES = ("?47l", "?1047l")

    def _left_early(self, leave, rows=4, fill=FILL4, save="3;2",
                    cursor="4;1"):
        screen = feed(Screen(rows, 8), fill + ESC + "[" + save + "H"
                      + ESC + "[?1049h" + ESC + "[" + leave
                      + ESC + "[" + cursor + "H")
        self.assertFalse(screen.alt_active, "前提: メイン画面へ戻っていない")
        return screen

    def test_1049l_after_shrinking_returns_to_the_saved_line(self):
        """メイン画面に居る間に縮めても、1049l が控えた行へ戻ること。"""
        for leave in self.LEAVES:
            with self.subTest(leave=leave):
                screen = self._left_early(leave)
                screen.set_size(2, 8)
                feed(screen, ESC + "[?1049l" + "!")
                self.assertEqual(text_of(screen), ["C!CC", "DDDD"],
                                 "1049l が控えた行の下の受信済みの行を潰した")

    def test_growing_back_does_not_land_on_a_blank_row(self):
        """縮めて元の高さへ戻したあとの 1049l が、下に足した空行へ行かないこと。"""
        for leave in self.LEAVES:
            with self.subTest(leave=leave):
                screen = self._left_early(leave)
                screen.set_size(2, 8)
                screen.set_size(4, 8)
                feed(screen, ESC + "[?1049l" + "!")
                self.assertEqual(text_of(screen),
                                 ["C!CC", "DDDD", "", ""])

    def test_dropping_only_blank_rows_keeps_the_1049_save(self):
        """下の空行を捨てるだけ (履歴へ送らない) なら、1049 の保存は動かないこと。

        DECSC の test_dropping_only_blank_rows_keeps_the_save と対の形。
        """
        for leave in self.LEAVES:
            with self.subTest(leave=leave):
                screen = self._left_early(
                    leave, fill=CRLF.join(["AAAA", "BBBB"]), save="2;2",
                    cursor="2;1")
                screen.set_size(2, 8)
                self.assertEqual(len(screen.history), 0,
                                 "前提: 下の空行を捨てるだけになっていない")
                feed(screen, ESC + "[?1049l" + "!")
                self.assertEqual(text_of(screen), ["AAAA", "B!BB"],
                                 "空行を捨てただけで 1049 の保存が上へずれ、"
                                 "控えた行より上の受信済みの行を潰した")

    def test_only_the_rows_pushed_to_history_shift_the_1049_save(self):
        """空行の切り捨てと履歴送りが混ざる縮小では、送った行の数だけずれること。"""
        for leave in self.LEAVES:
            with self.subTest(leave=leave):
                screen = self._left_early(
                    leave, rows=5, fill=FILL4, save="3;2", cursor="4;1")
                screen.set_size(3, 8)
                self.assertEqual(len(screen.history), 1,
                                 "前提: 空行 1 行を捨てて 1 行を履歴へ送る"
                                 "形になっていない")
                feed(screen, ESC + "[?1049l" + "!")
                self.assertEqual(text_of(screen), ["BBBB", "C!CC", "DDDD"],
                                 "1049l が控えた行へ戻らなかった (捨てた空行"
                                 "の分までずれると上、ずらさないと下の行を潰す)")


class WhatAlreadyWorkedStaysTheSameTest(unittest.TestCase):
    """直す前から正しかった形 (直したあとも変わらないこと)。"""

    def test_a_save_pushed_into_history_goes_to_the_top_row(self):
        """控えた行そのものが履歴へ出たら、0 行目へ戻ること。"""
        screen = feed(Screen(4, 8), FILL4 + ESC + "[1;2H" + DECSC
                      + ESC + "[4;3H")
        screen.set_size(2, 8)
        feed(screen, DECRC + "!")
        self.assertEqual(text_of(screen), ["C!CC", "DDDD"])

    def test_a_1049_save_pushed_into_history_goes_to_the_top_row(self):
        """47l のあとに残った 1049 の保存の行が履歴へ出たら、0 行目へ戻ること。"""
        screen = feed(Screen(4, 8), FILL4 + ESC + "[1;2H" + ESC + "[?1049h"
                      + ESC + "[?47l" + ESC + "[4;3H")
        screen.set_size(2, 8)
        feed(screen, ESC + "[?1049l" + "!")
        self.assertEqual(text_of(screen), ["C!CC", "DDDD"])

    def test_dropping_only_blank_rows_keeps_the_save(self):
        """下の空行を捨てるだけ (履歴へ送らない) なら、控えた行は動かないこと。"""
        screen = feed(Screen(4, 8), CRLF.join(["AAAA", "BBBB"])
                      + ESC + "[2;2H" + DECSC)
        screen.set_size(2, 8)
        feed(screen, DECRC + "!")
        self.assertEqual(text_of(screen), ["AAAA", "B!BB"])

    def test_a_save_made_on_the_alternate_screen_is_not_shifted(self):
        """代替画面の中の ESC 7 は、下を切るだけの代替画面に合わせて動かないこと。"""
        screen = feed(Screen(4, 8), FILL4 + ESC + "[?1049h" + ESC + "[2;1H"
                      + "a1a1" + ESC + "[2;2H" + DECSC)
        screen.set_size(2, 8)
        feed(screen, DECRC + "!")
        self.assertEqual(text_of(screen), ["", "a!a1"])


class SavedRowThroughTheWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_line_below_the_saved_one(self):
        """利用者が見る文書でも、ESC 8 の 1 文字が控えた行に載ること。"""
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        terminal = widget.create_terminal_tab("dev")
        screen = terminal._screen
        rows, cols = screen.rows, screen.cols
        lines = ["row%02d-%s" % (i, "x" * 8) for i in range(rows)]
        widget.queue_output("dev", CRLF.join(lines))
        widget._flush_pending_output()
        target = rows - 2
        widget.queue_output("dev", ESC + "[%d;7H" % (target + 1) + DECSC
                            + ESC + "[%d;1H" % rows)
        widget._flush_pending_output()
        # 窓を 3 行ぶん縦に縮める (_apply_grid_size の経路をそのまま通す。
        # 格子の大きさだけをフォントの寸法から切り離す)
        with mock.patch.object(widget, "_grid_size",
                               return_value=(rows - 3, cols)):
            widget._apply_grid_size("dev")
        self.assertEqual(screen.rows, rows - 3, "前提: 縮んでいない")
        widget.queue_output("dev", DECRC + "!")
        widget._flush_pending_output()
        hit = [ln for ln in terminal.toPlainText().split(chr(10))
               if "!" in ln]
        self.assertEqual(hit, ["row%02d-!%s" % (target, "x" * 7)],
                         "ESC 8 の 1 文字が控えた行の下の受信済みの行を潰した")


if __name__ == "__main__":
    unittest.main()
