r"""「最終桁へ印字した」覚えが、復元で戻り・入れ替えで落ちることを検証する。

折り返し無効 (ESC[?7l) のとき、右端で印字してもカーソルは動かず折り返し
待ちも立たないので、_join_previous は「最終桁に居る」ことと「最終桁へ
印字した」ことを区別できない。そこで直前の印字が最終桁へ届いたかを
_printed_at_last_col に覚えるようにしたが、覚えを落とすのが _move・
_ctrl (CR/BS/TAB)・_linefeed だけだったため、両側にずれが残っていた。

実測 (基準 aa38a2b、Screen(3, 4)、AC = U+0301):
  (a) 復元で最終桁へ戻る経路が、_move で覚えを落としたまま戻らない。
      8cd4aab の前は正しかったので退行。
        ESC[?7l 'ABcd' ESC 7 ESC[1;1H ESC 8 AC
          HEAD : ['A', 'B', 'ć', 'd']   (1 つ左へずれる)
          正   : ['A', 'B', 'c', 'd́']
        ESC[?1048h / ESC[?1048l 版、ESC[?1049h / ESC[?1049l 版も同じ。
  (b) 逆に、セルの中身が入れ替わる経路では覚えが残り続ける (8cd4aab
      の前からある穴で、退行ではない)。
        ESC[?7l 'ABC' ESC[2;1H 'abcd' ESC M AC
          HEAD : ['A', 'B', 'C', ' ́']   (RI は行を変えるのに落とさない)
          正   : ['A', 'B', 'ć', ' ']
        代替画面の出入り (1049h の白紙化・47h・47l)、ECH・EL・ED・
        ICH・DCH も同じ形で、受信していない空白へアクセントが付く。

直し方: 覚えを _pending_wrap と同じ扱いにする。(1) DECSC の保存領域
(_saved) と 1049 の保存 (_saved_main) の末尾へ足し、_save_cursor で
保存、_restore_cursor と _switch_screen の復元では _move のあとに戻す。
(2) RI・画面の入れ替え (代替画面にいるままの 1049h の白紙化を含む)・
ED/EL/ECH/ICH/DCH でも落とす。

残る制限: 桁の変わらない空振りの移動 (最終桁での TAB・同じ桁への CUP)
は _move / _ctrl が無条件に覚えを落とすので、8cd4aab の前より 1 つ左へ
ずれたままになる。真偽値ではなく「最後に書いたセルの位置」を覚える形
にしないと直らず、それは
tests/test_terminal_combining_at_last_col_no_autowrap.py の CUP の期待値
と食い違うため、ここでは触らない。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
NO_WRAP = ESC + "[?7l"
AC = chr(0x0301)
SO = chr(0x0E)
SI = chr(0x0F)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def cells_of(screen, row=0):
    return [cell[0] for cell in screen.lines[row]]


class TheMemoComesBackWithTheCursorTest(unittest.TestCase):
    """復元でカーソルを最終桁へ戻したら、覚えも一緒に戻ること。"""

    def test_decrc_brings_the_memo_back(self):
        """ESC 8 で戻った最終桁の文字へ、アクセントが付くこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "7"
                      + ESC + "[1;1H" + ESC + "8" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c", "d" + AC])

    def test_1048l_brings_the_memo_back(self):
        """?1048l でも同じこと (保存領域は DECSC と共通)。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[?1048h"
                      + ESC + "[1;1H" + ESC + "[?1048l" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c", "d" + AC])

    def test_1049l_brings_the_memo_back(self):
        """?1049l でも同じこと (1049 用の保存から戻す)。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[?1049h"
                      + ESC + "[?1049l" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c", "d" + AC])

    def test_a_restore_without_a_printed_last_col_does_not_invent_one(self):
        """最終桁へ印字していない位置を保存したなら、戻しても付かないこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABc" + ESC + "[1;4H"
                      + ESC + "7" + ESC + "[1;1H" + ESC + "8" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c" + AC, " "])


class TheMemoIsDroppedWhenTheCellIsGoneTest(unittest.TestCase):
    """セルの中身が入れ替わる経路では、覚えを落とすこと。"""

    def test_reverse_index_drops_the_memo(self):
        """ESC M で別の行へ移ったら、その行の最終桁は空白なこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABC" + ESC + "[2;1H"
                      + "abcd" + ESC + "M" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "C" + AC, " "])

    def test_entering_the_alternate_screen_drops_the_memo(self):
        """1049h の白紙の代替画面には、印字済みのセルが無いこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[?1049h" + AC)
        self.assertEqual(cells_of(screen), [" ", " ", " " + AC, " "])

    def test_entering_through_47h_drops_the_memo(self):
        """47h でも画面が入れ替わるので落とすこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[?47h" + AC)
        self.assertEqual(cells_of(screen), [" ", " ", " " + AC, " "])

    def test_clearing_the_alternate_screen_again_drops_the_memo(self):
        """代替画面にいるまま 1049h を受けて白紙にしたときも落とすこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[?1049h"
                      + "wxyz" + ESC + "[?1049h" + AC)
        self.assertEqual(cells_of(screen), [" ", " ", " " + AC, " "])

    def test_47l_back_to_the_main_screen_drops_the_memo(self):
        """47l はカーソルを戻さないので、代替画面の覚えを持ち帰らないこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABC" + ESC + "[?47h"
                      + "wxyz" + ESC + "[?47l" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "C" + AC, " "])

    def test_erase_chars_drops_the_memo(self):
        """ECH で消したセルは、もう印字済みではないこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[X" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c" + AC, " "])

    def test_erase_line_drops_the_memo(self):
        """EL 0 で消したセルも同じこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[0K" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c" + AC, " "])

    def test_erase_display_drops_the_memo(self):
        """ED 1 で消したセルも同じこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[1J" + AC)
        self.assertEqual(cells_of(screen), [" ", " ", " " + AC, " "])

    def test_delete_chars_drops_the_memo(self):
        """DCH で詰めた行の最終桁は、埋め草の空白なこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[P" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c" + AC, " "])

    def test_insert_chars_drops_the_memo(self):
        """ICH で押し出した最終桁も同じこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + ESC + "[@" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c" + AC, " "])


class TheMemoIsUnchangedElsewhereTest(unittest.TestCase):
    """対照: 触っていない経路が変わっていないこと。"""

    def test_printing_into_the_last_column_still_takes_the_accent(self):
        """最終桁まで書いたら、その最終桁の文字へ付くこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c", "d" + AC])

    def test_stopping_before_the_last_column_still_takes_the_accent(self):
        """最終桁の手前で止まったら、書いた文字へ付くこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABe" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "e" + AC, " "])

    def test_so_and_si_keep_the_memo(self):
        """SO / SI は位置を変えないので、覚えを落とさないこと。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd" + SO + SI + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c", "d" + AC])

    def test_a_carriage_return_still_drops_the_memo(self):
        """CR で行頭へ戻ったあとは、これまでどおり捨てること。"""
        screen = feed(Screen(3, 4), NO_WRAP + "ABcd\r" + AC)
        self.assertEqual(cells_of(screen), ["A", "B", "c", "d"])


if __name__ == "__main__":
    unittest.main()
