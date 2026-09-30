r"""折り返しを有効へ戻した直後の結合文字が、1 つ左の文字へ付く件を検証する。

_join_previous は「直前の印字が最終桁へ届いた」覚え (_printed_at_last_col)
を、折り返しが無効 (ESC[?7l) のときにしか見ていなかった。?7l のまま最終桁
まで印字してから ?7h で折り返しを戻すと、覚えは残っているのに参照されず、
続く結合文字が最終桁の文字ではなく 1 つ左の文字へ付いた。

実測 (基準 441ea02 = v1.3.1、AC = U+0301):
  Screen(2, 4) へ ESC[?7l 'ABcd' ESC[?7h AC
    HEAD : ['A', 'B', 'c' + AC, 'd']   (1 つ左の c へ付く)
    正   : ['A', 'B', 'c', 'd' + AC]
  ?7h を挟まなければ d に付く。xterm では DECSET 7 は折り返しの切り替え
  だけで、結合文字は最後に書いたセルへ付く (char_was_written を落とすのは
  ResetWrap だけ) ので、d に付く。

直し方: 覚えを折り返しの有無に関わらず使う (at_last_col から
`not self.autowrap` を外す)。折り返し有効で最終桁まで印字したときは
折り返し待ちが立つので、ふだんの結果は変わらない。

それだけだと、右端で折り返し待ちのまま窓を 1 桁だけ広げたとき (set_size が
待ちを解き、カーソルは印字したセルの 1 つ右 = 新しい最終桁へ出る) に、
覚えが残ったまま最終桁に居ることになり、結合文字が広げて足した空白へ
付くようになる (441ea02 では正しく印字したセルへ付く)。set_size が待ちを
解いたときは、カーソルはもう印字したセルに居ないので覚えも落とす。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
CSI = ESC + "["
AC = chr(0x0301)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def cells_of(screen, row=0):
    return [cell[0] for cell in screen.lines[row]]


class CombiningAfterAutowrapBackOnTest(unittest.TestCase):
    def test_the_accent_joins_the_last_printed_cell(self):
        """?7l で最終桁まで印字 -> ?7h -> 結合文字は d に付くこと (xterm と同じ)。"""
        screen = feed(Screen(2, 4), CSI + "?7lABcd" + CSI + "?7h" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c", "d" + AC])

    def test_an_unprinted_last_column_is_still_skipped(self):
        """対照: 最終桁へ印字していなければ、これまでどおり 1 つ左へ付くこと。"""
        screen = feed(Screen(2, 4), CSI + "?7lABc" + CSI + "?7h" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c" + AC, " "])

    def test_the_next_character_still_overwrites_the_last_column(self):
        """対照: ?7h のあとの印字は、これまでどおり右端へ重ねて書くこと。"""
        screen = feed(Screen(2, 4), CSI + "?7lABcd" + CSI + "?7hX")
        self.assertEqual(cells_of(screen), ["A", "B", "c", "X"])


class WideningByOneColumnTest(unittest.TestCase):
    """折り返し待ちのまま 1 桁だけ広げても、印字したセルへ付くこと。"""

    def test_the_accent_does_not_move_onto_the_padding(self):
        """'ABCD' (待ち) -> 5 桁へ広げる -> 結合文字は D に付くこと。"""
        screen = feed(Screen(2, 4), "ABCD")
        screen.set_size(2, 5)
        feed(screen, AC)
        self.assertEqual(cells_of(screen), ["A", "B", "C", "D" + AC, " "])

    def test_widening_by_more_columns_is_unchanged(self):
        """対照: 2 桁以上広げたときも D に付くこと。"""
        screen = feed(Screen(2, 4), "ABCD")
        screen.set_size(2, 8)
        feed(screen, AC)
        self.assertEqual(cells_of(screen)[:5], ["A", "B", "C", "D" + AC, " "])

    def test_a_rows_only_change_keeps_the_wait(self):
        """対照: 行数だけ変えたときは待ちが残り、D に付くこと。"""
        screen = feed(Screen(2, 4), "ABCD")
        screen.set_size(3, 4)
        feed(screen, AC)
        self.assertEqual(cells_of(screen), ["A", "B", "C", "D" + AC])


if __name__ == "__main__":
    unittest.main()
