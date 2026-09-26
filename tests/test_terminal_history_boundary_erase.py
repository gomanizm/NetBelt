r"""画面の消去で 0 行目が丸ごと消えても、履歴の最後の行が次の出力へ繋がる件を検証する。

1.3.2 の 1 周目で、履歴へ送った最後の行が折り返しで 0 行目へ続いているとき
(_history_open)、0 行目へ別の行が来たら閉じるようにした (IL・上端の SD と RI・
行頭からの印字。tests/test_terminal_history_boundary_break.py)。ところが、画面の
消去が 0 行目を空行へ置き換える経路 (_erase_display の置き換えの輪と RIS) からは
閉じていなかった。消したあとに 0 行目の行頭以外へ書くと _break_history も通らず、
履歴の行と新しい行が 1 行に繋がって、コピーと全ログ保存の文書に残る。

実測 (基準 c2bb66a):
  Screen(3, 4) へ 'ABCDEFGHIJKLM' (ABCD は折り返しの印付きで履歴へ) のあと
    ESC[2;1H ESC[1J ESC[1;3H 'Z'  (ED 1)     論理行 'ABCD  Z\n JKLM\n'
    ESC[2;3H ESC[1J ESC[1;3H 'Z'  (ED 1)     'ABCD  Z\n   LM\n'
    ESC[2;1H ESC[1J               (ED 1 だけ) 'ABCD\n JKLM\n' (0 行目の空行が消える)
    どれも描画側へ渡し済みなら take_history_break() は False のまま。
  0 行目を EL で消して画面が丸ごと空白になったあと (記録する中身が無い) の
  ESC[2J・原点からの ESC[J・3 行目の ESC[1J・ESC c (RIS) も同じく
    ESC[1;3H 'Z' で 'ABCD  Z\n\n\n'。
  TerminalWidget (3x10) へ a*10 b*10 c*10 d*5 のあと ESC[2;1H ESC[1J ESC[1;3H 'new'
    unwrapped_text の 1 行目 'aaaaaaaaaa  new'。

直し方: 0 行目から始まる範囲を空行へ置き換えた ED (ED 1 のカーソルが 1 行目
より下のとき・ED 2・原点からの ED 0) のあとと、RIS で画面を白紙にする前に
_break_history を呼ぶ。消した行の折り返しの印は消去で落ちる (xterm の
ClearBufRows も消した行の印を落とす)。0 行目の上は履歴の最後の行で、その
続きの 0 行目が消えたのだから、IL・SD で 0 行目へ別の行が来たときと同じく
閉じる。画面の中でも、ED 0 は置き換えた範囲の直前の行 (カーソル行) の印を外して
いるので、それと揃う。中身のある画面を丸ごと消すときは、もともと _record_screen
が記録して閉じていたので変わらない。
変えないもの: 0 行目を消し切らない ED 1 (カーソルが 0 行目) と EL は、行を
その場で消すだけで上の行の印に触らない (画面の中の行と同じ)。ED 3 は画面に
触らない。代替画面の中では閉じない (1 周目の決定 (a))。
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

# 3x4 の画面で 1 行目 'ABCD' が折り返しの印付きで履歴へ出る。画面は
# 'EFGH' (印) / 'IJKL' (印) / 'M'
FILL = "ABCDEFGHIJKLM"
# 画面の 3 行を EL 2 で空にする。0 行目は印ごと消えるが、履歴の行はまだ開いた
# まま (EL は上の行の印に触らない)。このあとの画面の消去には記録する中身が無い
BLANK_ROWS = (CSI + "1;1H" + CSI + "2K" + CSI + "2;1H" + CSI + "2K"
              + CSI + "3;1H" + CSI + "2K")


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


class EraseClosesHistoryLineTest(unittest.TestCase):
    CASES = (
        # 0 行目を空行へ置き換えたあと、0 行目の行頭以外へ書く (_print の
        # 行頭の枝を通らないので、消去の側で閉じないと繋がる)
        ("ED 1 at row 1, col 1", CSI + "2;1H" + CSI + "1J" + CSI + "1;3HZ",
         "ABCD\n  Z\n JKLM\n"),
        ("ED 1 at row 1, col 3", CSI + "2;3H" + CSI + "1J" + CSI + "1;3HZ",
         "ABCD\n  Z\n   LM\n"),
        ("ED 1 alone", CSI + "2;1H" + CSI + "1J", "ABCD\n\n JKLM\n"),
        # 画面が丸ごと空白で、消去の前に記録する中身が無いとき
        ("ED 2 on a blank screen",
         BLANK_ROWS + CSI + "2J" + CSI + "1;3HZ", "ABCD\n  Z\n\n\n"),
        ("ED 0 from home on a blank screen",
         BLANK_ROWS + CSI + "H" + CSI + "J" + CSI + "1;3HZ",
         "ABCD\n  Z\n\n\n"),
        ("ED 1 at the bottom on a blank screen",
         BLANK_ROWS + CSI + "3;4H" + CSI + "1J" + CSI + "1;3HZ",
         "ABCD\n  Z\n\n\n"),
        ("RIS on a blank screen",
         BLANK_ROWS + ESC + "c" + CSI + "1;3HZ", "ABCD\n  Z\n\n\n"),
    )

    def test_erasing_row_zero_ends_the_history_line(self):
        """0 行目を丸ごと消す ED・RIS のあと、履歴の行を新しい行へ繋げないこと。"""
        for name, seq, want in self.CASES:
            with self.subTest(name):
                screen = feed(Screen(3, 4), FILL + seq)
                self.assertEqual(logical(screen), want)

    def test_a_history_line_already_handed_over_is_closed_by_the_renderer(self):
        """描画側へ渡し済みの履歴の行は、描画側へ閉じるよう知らせること (1 回だけ)。"""
        for name, seq, _ in self.CASES:
            with self.subTest(name):
                screen = feed(Screen(3, 4), FILL)
                self.assertEqual(screen.take_new_history()[-1][1], True,
                                 "前提: 履歴の最後の行は 0 行目へ続いている")
                self.assertFalse(screen.take_history_break())
                feed(screen, seq)
                self.assertTrue(screen.take_history_break())
                self.assertFalse(screen.take_history_break())

    def test_a_blank_screen_really_had_nothing_to_record(self):
        """前提: 空白の画面の件は、消去の前の記録 (_record_screen) では閉じていないこと。"""
        screen = feed(Screen(3, 4), FILL)
        screen.take_new_history()
        feed(screen, BLANK_ROWS)
        self.assertEqual(screen.text(), ["", "", ""])
        self.assertEqual(screen.take_new_history(), [])
        self.assertFalse(screen.take_history_break())

    # 対照: 0 行目がその場で消えるだけの形は、画面の中の行と同じく繋がったまま
    KEPT_CASES = (
        # カーソルが 0 行目の ED 1 は、0 行目の左側を消すだけ。残った 'GH' が
        # 続きのまま (EL 1 と同じ)
        ("ED 1 inside row 0", CSI + "1;2H" + CSI + "1J" + CSI + "1;3HZ",
         "ABCD  ZH\nIJKLM\n"),
        # 原点以外からの ED 0 も、0 行目の左側 'EF' が続きとして残る
        ("ED 0 inside row 0", CSI + "1;3H" + CSI + "J" + CSI + "1;4HZ",
         "ABCDEF Z\n\n\n"),
        # EL は行をその場で消す。画面の中でも上の行の印には触らない
        ("EL 2 on row 0", CSI + "H" + CSI + "2K" + CSI + "1;3HZ",
         "ABCD  Z\nIJKLM\n"),
        # ED 3 は履歴を消す命令で、画面にも記録にも触らない
        ("ED 3", CSI + "3J" + CSI + "1;3HZ", "ABCDEFZH\nIJKLM\n"),
    )

    def test_erasing_in_place_still_joins(self):
        """対照: 0 行目を消し切らない ED 1・EL・ED 3 では、今までどおり繋がったままのこと。"""
        for name, seq, want in self.KEPT_CASES:
            with self.subTest(name):
                screen = feed(Screen(3, 4), FILL + seq)
                self.assertEqual(logical(screen), want)
                screen = feed(Screen(3, 4), FILL)
                screen.take_new_history()
                feed(screen, seq)
                self.assertFalse(screen.take_history_break())

    def test_a_screen_with_content_is_recorded_as_before(self):
        """対照: 中身のある画面を消す ED 2 は、今までどおり記録してから閉じること。"""
        screen = feed(Screen(3, 4), FILL + CSI + "2J" + CSI + "1;3HZ")
        self.assertEqual(logical(screen), "ABCDEFGHIJKLM\n  Z\n\n\n")

    def test_the_alternate_screen_does_not_close_the_line(self):
        """代替画面の中の ED 1・ED 2 では閉じず、戻ったあとも続きのまま繋がること。"""
        seq = (CSI + "?1049h" + CSI + "2;1H" + CSI + "1J" + CSI + "2J"
               + CSI + "1;3Hvi" + CSI + "?1049l")
        screen = feed(Screen(3, 4), FILL + seq)
        self.assertEqual(logical(screen), "ABCDEFGHIJKLM\n")
        screen = feed(Screen(3, 4), FILL)
        screen.take_new_history()
        feed(screen, seq)
        self.assertFalse(screen.take_history_break())


class EraseClosesHistoryLineThroughWidgetTest(unittest.TestCase):
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

    FILL = "a" * 10 + "b" * 10 + "c" * 10 + "d" * 5
    # 0 行目 ('b' * 10) を ED 1 で消し、1 行目の 1 桁目も消える
    REST = " " + "c" * 9 + "d" * 5

    def _document(self, pieces):
        widget, terminal = self._terminal(3, 10)
        for piece in pieces:
            widget.append_output("dev", piece)
        # 画面を履歴へ押し出して、文書の記録の側へ確定させる
        widget.append_output("dev", CSI + "3;1H" + CRLF * 3 + "end")
        return terminal.unwrapped_text().split(NL)

    def test_new_output_after_erase_above_is_not_glued_to_the_history(self):
        """文書 (コピー・全ログ保存の元) で、ED 1 のあとの行が過去の行へ繋がらないこと。"""
        seq = CSI + "2;1H" + CSI + "1J" + CSI + "1;3Hnew"
        for name, pieces in (("handed over", (self.FILL, seq)),
                             ("one piece", (self.FILL + seq,))):
            with self.subTest(name):
                self.assertEqual(self._document(pieces)[:3],
                                 ["a" * 10, "  new", self.REST])

    def test_the_erased_row_zero_stays_an_empty_line(self):
        """ED 1 で空になった 0 行目は、過去の行へ吸い込まれず空行として残ること。"""
        lines = self._document((self.FILL, CSI + "2;1H" + CSI + "1J"))
        self.assertEqual(lines[:3], ["a" * 10, "", self.REST])


if __name__ == "__main__":
    unittest.main()
