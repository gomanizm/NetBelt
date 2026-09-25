r"""折り返し無効のとき、結合文字が右隣の空白へ付く件を検証する。

_join_previous は「折り返しが無効 (ESC[?7l) なら、最終桁で印字しても
カーソルが動かない」ことを当て込んで、最終桁に居るだけで「その桁へ印字
済み」と見なしていた。印字が最終桁まで届いていなくても最終桁へ着いた
だけ (印字で進んだ・TAB で止まった・CUP で来た) なら、まだ何も書いて
いない空白のセルへアクセントが付く。

実測 (基準 f4cad23):
  Screen(2, 4) へ ESC[?7l + 'ABe' + U+0301
    HEAD : ['A', 'B', 'e', ' ' + U+0301]   (アクセントが空白へ付く)
    正   : ['A', 'B', 'e' + U+0301, ' ']
  'e' は桁 2 へ書かれ、end (3) < cols (4) なのでカーソルは桁 3 へ進む
  だけで、桁 3 は空白のまま。近道 (_print_narrow) と 1 文字経路
  (_print_chars) のどちらでも同じなので、経路差の試験では出ない。
  ESC[?7l + 'ABe' + TAB + U+0301 も、docstring が「残る制限」と書いて
  いた ESC[?7l + 'ABcd' + ESC[1;4H + U+0301 も同じ形。
  折り返し有効 (既定) のときは右端で折り返し待ちが立つので正しい。
  TerminalWidget の文書 (既定 49 桁) にもそのまま出る。
    HEAD : 48 桁ぶん受けたあと '...567 ́' (49 桁目の空白にアクセント)
    正   : '...567́'  (48 桁目の '7' にアクセント)

直し方: 直前の印字が最終桁へ着いたか (end >= cols) を覚え、最終桁に
居ることと合わせて判定する。覚えは _print_chars / _print_narrow の
書き終わりで更新し、カーソルを動かす _move・_ctrl (CR/BS/TAB)・
_linefeed で落とす。これで「CUP で最終桁へ移動した場合」の制限も
無くなるので、docstring からその一文を外した。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
NO_WRAP = ESC + "[?7l"
COMB_ACUTE = chr(0x0301)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def cells_of(screen, row=0):
    return [cell[0] for cell in screen.lines[row]]


def ruler(n):
    return "".join(str(i % 10) for i in range(n))


class CombiningCharNeedsAPrintedCellTest(unittest.TestCase):
    def test_the_accent_lands_on_the_last_printed_cell(self):
        """印字で最終桁へ進んだだけなら、書いた文字へ付くこと。"""
        screen = feed(Screen(2, 4), NO_WRAP + "ABe" + COMB_ACUTE)
        self.assertEqual(cells_of(screen),
                         ["A", "B", "e" + COMB_ACUTE, " "])

    def test_a_tab_to_the_last_column_does_not_take_the_accent(self):
        """TAB で最終桁に止まっただけでも、書いた文字へ付くこと。"""
        screen = feed(Screen(2, 4), NO_WRAP + "ABe\t" + COMB_ACUTE)
        self.assertEqual(cells_of(screen),
                         ["A", "B", "e" + COMB_ACUTE, " "])

    def test_a_cup_to_the_last_column_does_not_take_the_accent(self):
        """CUP で最終桁へ来ただけなら、1 つ左の文字へ付くこと。"""
        screen = feed(Screen(2, 4),
                      NO_WRAP + "ABcd" + ESC + "[1;4H" + COMB_ACUTE)
        self.assertEqual(cells_of(screen),
                         ["A", "B", "c" + COMB_ACUTE, "d"])

    def test_a_wide_char_before_the_last_column(self):
        """全角を書いて最終桁へ進んだときも、その全角へ付くこと。"""
        screen = feed(Screen(2, 4), NO_WRAP + "A" + "漢" + COMB_ACUTE)
        self.assertEqual(cells_of(screen),
                         ["A", "漢" + COMB_ACUTE, "", " "])

    def test_the_slow_path_behaves_the_same(self):
        """1 文字経路 (IRM) でも同じ結果になること。"""
        screen = feed(Screen(2, 4),
                      NO_WRAP + ESC + "[4h" + "ABe" + COMB_ACUTE)
        self.assertEqual(cells_of(screen),
                         ["A", "B", "e" + COMB_ACUTE, " "])


class CombiningCharAtTheLastColumnIsUnchangedTest(unittest.TestCase):
    """最終桁へ印字した正しい場合が、変わっていないことの対照。"""

    def test_printing_into_the_last_column_still_takes_the_accent(self):
        """最終桁まで書いたら、その最終桁の文字へ付くこと。"""
        screen = feed(Screen(2, 4), NO_WRAP + "ABcd" + COMB_ACUTE)
        self.assertEqual(cells_of(screen),
                         ["A", "B", "c", "d" + COMB_ACUTE])

    def test_overwriting_at_the_last_column_still_takes_the_accent(self):
        """最終桁で上書きを続けたあとも、最終桁の文字へ付くこと。"""
        screen = feed(Screen(2, 4), NO_WRAP + "ABcdef" + COMB_ACUTE)
        self.assertEqual(cells_of(screen),
                         ["A", "B", "c", "f" + COMB_ACUTE])

    def test_a_wide_char_pushed_back_from_the_last_column(self):
        """右端に入らず手前へ重ねた全角にも、これまでどおり付くこと。"""
        screen = feed(Screen(2, 4), NO_WRAP + "ABC" + "漢" + COMB_ACUTE)
        self.assertEqual(cells_of(screen),
                         ["A", "B", "漢" + COMB_ACUTE, ""])

    def test_autowrap_on_is_unchanged(self):
        """対照: 折り返し有効のときは元から正しいこと。"""
        screen = feed(Screen(2, 4), "ABe" + COMB_ACUTE)
        self.assertEqual(cells_of(screen),
                         ["A", "B", "e" + COMB_ACUTE, " "])

    def test_a_carriage_return_still_drops_the_accent(self):
        """CR で行頭へ戻ったあとは、これまでどおり捨てること。"""
        screen = feed(Screen(2, 4), NO_WRAP + "ABe\r" + COMB_ACUTE)
        self.assertEqual(cells_of(screen), ["A", "B", "e", " "])

    def test_a_short_screen_is_unchanged(self):
        """対照: 最終桁の外なら元から正しいこと。"""
        screen = feed(Screen(2, 8), NO_WRAP + "ABe" + COMB_ACUTE)
        self.assertEqual(cells_of(screen)[:4],
                         ["A", "B", "e" + COMB_ACUTE, " "])


class CombiningCharThroughTheWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_does_not_grow_an_accented_blank(self):
        """利用者が見る文書でも、空白にアクセントが付かないこと。"""
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        terminal = widget.create_terminal_tab("dev")
        cols = terminal._screen.cols
        widget.queue_output("dev", NO_WRAP + ruler(cols - 1) + COMB_ACUTE)
        widget._flush_pending_output()
        rows = terminal.toPlainText().split("\n")
        self.assertEqual(rows[0], ruler(cols - 1) + COMB_ACUTE,
                         "受信していない空白へアクセントが付いている")


if __name__ == "__main__":
    unittest.main()
