r"""右端で折り返し待ちの瞬間に保存 -> 窓を広げる -> 復元 -> 出力で、論理行へ空白が混ざる件を検証する。

set_size は、折り返しの印が無い行を新しい桁まで空白で埋める。待ち中の行は
まだ印が付いていないので埋め草が入る。復元 (ESC 8・?1048l・?1049l) は、
桁が保存時以上なら待ちを残す (xterm も DECRC で待ちを戻す)。続く 1 文字が
待ちから _linefeed(from_wrap=True) を呼ぶと、行を「新しい桁」で切り詰める
ので、埋め草が折り返し行の中身として残った。描画側は折り返し行を刈らずに
次の行と繋ぐので、コピーと全ログ保存で 1 本の行の途中へ空白が入る。

実測 (基準 441ea02 = v1.3.1):
  Screen(3, 4) へ 'ABCD' ESC 7 → set_size(3, 8) → ESC 8 'X'
    HEAD : 0 行目のセル 'ABCD    '、wrapped [True, False, False]、
           論理行 'ABCD    X'
    正   : 0 行目 'ABCD'、論理行 'ABCDX'
  ?1048h/l、?1049h/l でも同じ。保存を挟まずに広げると 'ABCDX'。
  TerminalWidget (5x20 → 49 桁) では unwrapped_text の 1 行目が
    'ssh-rsa AAAAB3NzaC1y' + 空白 29 個 + 'c2EAAAADAQAB user@example.com'
  になり、公開鍵の途中に空白が入った。X が次の行へ行く点は xterm と同じ。

直し方: 待ちから折り返すとき、待っていた桁 (wrap_col) を _linefeed へ渡す。
その右 (wrap_col + 1 から桁まで) が埋め草の空白だけのときに限り、
wrap_col + 1 で切る。それ以外は今までどおり桁で切る。広げたあとに同じ行の
右へ受信した文字 (Q や色付きの空白) は消さない。生きているカーソルの待ちは
いつも右端 (桁 - 1) で立つので、ふだんの折り返しは変わらない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
CSI = ESC + "["
CRLF = chr(13) + chr(10)
NL = chr(10)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def row_text(screen, r):
    return "".join(cell[0] for cell in screen.lines[r])


class SavedWrapAfterWideningTest(unittest.TestCase):
    PAIRS = (("DECSC", ESC + "7", ESC + "8"),
             ("1048", CSI + "?1048h", CSI + "?1048l"),
             ("1049", CSI + "?1049h", CSI + "?1049l"))

    def test_the_wrapped_row_ends_where_the_wait_was(self):
        """待ちで折り返した行は、待っていた桁で終わること (埋め草を残さない)。"""
        for name, save, restore in self.PAIRS:
            with self.subTest(name):
                screen = feed(Screen(3, 4), "ABCD" + save)
                screen.set_size(3, 8)
                feed(screen, restore + "X")
                self.assertTrue(screen.wrapped[0])
                self.assertEqual(row_text(screen, 0), "ABCD",
                                 "折り返した行へ埋め草の空白が残っている")
                self.assertEqual(row_text(screen, 1).rstrip(), "X")

    def test_a_continuation_written_one_by_one_is_trimmed_too(self):
        """復元のあとの 1 文字目を 1 文字ずつ書く経路 (全角・DEC 罫線・IRM) でも、埋め草を残さないこと。"""
        # 半角の続き ('X' など) は _print_narrow を通る。全角・DEC 罫線・
        # 挿入モードは _print_chars の待ちの枝を通るので、別に見張る
        continuations = (("wide", chr(0x3042), chr(0x3042)),
                         ("DEC graphics", ESC + "(0q", chr(0x2500)),
                         ("IRM", CSI + "4hX", "X"))
        for name, save, restore in self.PAIRS:
            for kind, text, first in continuations:
                with self.subTest(name + " / " + kind):
                    screen = feed(Screen(3, 4), "ABCD" + save)
                    screen.set_size(3, 8)
                    feed(screen, restore + text)
                    self.assertTrue(screen.wrapped[0])
                    self.assertEqual(row_text(screen, 0), "ABCD",
                                     "折り返した行へ埋め草の空白が残っている")
                    self.assertEqual(screen.lines[1][0][0], first)

    def test_a_wide_character_at_the_edge_keeps_both_halves(self):
        """右端の全角で待っていたときも、全角を丸ごと残して埋め草だけ落とすこと。"""
        screen = feed(Screen(3, 4), "AB" + chr(0x3042) + ESC + "7")
        screen.set_size(3, 8)
        feed(screen, ESC + "8X")
        self.assertEqual([c[0] for c in screen.lines[0]],
                         ["A", "B", chr(0x3042), ""])

    def test_text_written_after_widening_is_kept(self):
        """広げたあとに同じ行の右へ書かれた文字は、折り返しでも消さないこと。"""
        screen = feed(Screen(3, 4), "ABCD" + ESC + "7")
        screen.set_size(3, 8)
        feed(screen, CSI + "1;7HQ" + ESC + "8X")
        self.assertEqual(row_text(screen, 0), "ABCD  Q ",
                         "受信した Q が消えた")

    def test_coloured_blanks_written_after_widening_are_kept(self):
        """色の付いた空白も受信した中身なので、消さないこと。"""
        screen = feed(Screen(3, 4), "ABCD" + ESC + "7")
        screen.set_size(3, 8)
        feed(screen, CSI + "1;6H" + CSI + "41m " + CSI + "0m" + ESC + "8X")
        self.assertEqual(len(screen.lines[0]), 8)
        self.assertNotEqual(screen.lines[0][5][1], screen.lines[0][4][1])

    def test_a_live_wait_still_wraps_at_the_edge(self):
        """対照: 保存を挟まない右端の待ちは、これまでどおり折り返すこと。"""
        screen = feed(Screen(3, 4), "ABCDX")
        self.assertEqual([row_text(screen, 0), row_text(screen, 1)],
                         ["ABCD", "X   "])
        self.assertTrue(screen.wrapped[0])

    def test_widening_alone_does_not_wrap(self):
        """対照: 保存を挟まずに広げたら、待ちは解けて同じ行へ続けること。"""
        screen = feed(Screen(3, 4), "ABCD")
        screen.set_size(3, 8)
        feed(screen, "X")
        self.assertEqual(row_text(screen, 0), "ABCDX   ")
        self.assertFalse(screen.wrapped[0])


class SavedWrapAfterWideningThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_copied_line_has_no_gap(self):
        """コピー・全ログ保存の元 (unwrapped_text) で、鍵の途中に空白が入らないこと。"""
        from ui.terminal_widget import TerminalWidget
        for name, save, restore in SavedWrapAfterWideningTest.PAIRS:
            with self.subTest(name):
                widget = TerminalWidget()
                self.addCleanup(widget.close)
                terminal = widget.create_terminal_tab("dev")
                terminal._screen.set_size(5, 20)
                widget._render_screen(terminal)
                widget.append_output("dev", "ssh-rsa AAAAB3NzaC1y" + save)
                terminal._screen.set_size(5, 49)
                widget._render_screen(terminal)
                widget.append_output("dev", restore
                                     + "c2EAAAADAQAB user@example.com"
                                     + CRLF + "$ ")
                first = terminal.unwrapped_text().split(NL)[0]
                self.assertEqual(
                    first, "ssh-rsa AAAAB3NzaC1yc2EAAAADAQAB user@example.com")

    def test_a_wide_continuation_has_no_gap_either(self):
        """復元のあとの続きが全角で始まっても、文書の行の途中に空白が入らないこと。"""
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        terminal = widget.create_terminal_tab("dev")
        terminal._screen.set_size(5, 20)
        widget._render_screen(terminal)
        widget.append_output("dev", "ssh-rsa AAAAB3NzaC1y" + ESC + "7")
        terminal._screen.set_size(5, 49)
        widget._render_screen(terminal)
        widget.append_output("dev", ESC + "8" + chr(0x3042)
                             + "c2EA user@example.com" + CRLF + "$ ")
        first = terminal.unwrapped_text().split(NL)[0]
        self.assertEqual(
            first, "ssh-rsa AAAAB3NzaC1y" + chr(0x3042) + "c2EA user@example.com")


if __name__ == "__main__":
    unittest.main()
