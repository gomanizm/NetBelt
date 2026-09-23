r"""SU / SD で最終桁のセルが入れ替わったら、「最終桁へ印字した」覚えを落とすことを検証する。

9597d83 で、RI・代替画面の出入り・ED/EL/ECH/ICH/DCH のようにカーソルの
下のセルが入れ替わる経路では _printed_at_last_col を落とすようにしたが、
SU (CSI S) と SD (CSI T) だけが漏れていた。どちらもカーソルを動かさず、
スクロール範囲の行を上下へずらす。カーソルの居る桁のセルは別の行の
ものに入れ替わるのに覚えが残るので、折り返し無効 (ESC[?7l) で最終桁へ
印字したあとの結合文字が、受信していない空白や、別の行から上がって
きた文字へ付く。

実測 (基準 097550c、AC = U+0301):
  Screen(3, 4): ESC[?7l 'ABcd' ESC[S AC
    HEAD : row0 [' ', ' ', ' ', ' ́']   (空白へアクセント)
    正   : row0 [' ', ' ', ' ́', ' ']   (1049h・RI・ED などと同じ形)
  ESC[T 版も同じ。下の行に 'wxyz' があると、SU で上がってきた 'z' に
  アクセントが付いて 'wxyź' になる ('z' はこの位置で受信していない)。

直し方: CSI S / CSI T の枝で、カーソルの行がスクロール範囲の中にある
ときだけ覚えを落とす。範囲の外の行は SU / SD で動かないので、そこで
印字した覚えはそのまま正しい (落とすと結合文字が 1 つ左へずれる)。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
CSI = ESC + "["
NO_WRAP = CSI + "?7l"
AC = chr(0x0301)
SU = CSI + "S"
SD = CSI + "T"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def cells_of(screen, row=0):
    return [cell[0] for cell in screen.lines[row]]


class ScrollDropsTheMemoTest(unittest.TestCase):
    def test_su_does_not_attach_to_a_blank_last_cell(self):
        """SU で空いた最終桁の空白へ、アクセントを付けないこと。"""
        s = feed(Screen(3, 4), NO_WRAP + "ABcd" + SU + AC)
        self.assertEqual(cells_of(s), [" ", " ", " " + AC, " "])

    def test_sd_does_not_attach_to_a_blank_last_cell(self):
        """SD で空行が割り込んだ最終桁の空白へ、アクセントを付けないこと。"""
        s = feed(Screen(3, 4), NO_WRAP + "ABcd" + SD + AC)
        self.assertEqual(cells_of(s), [" ", " ", " " + AC, " "])
        self.assertEqual(cells_of(s, 1), ["A", "B", "c", "d"])

    def test_su_does_not_attach_to_a_char_from_the_row_below(self):
        """SU で下から上がってきた文字へ、アクセントを付けないこと。"""
        s = feed(Screen(3, 4), NO_WRAP + CSI + "2;1H" + "wxyz"
                 + CSI + "1;1H" + "ABcd" + SU + AC)
        self.assertEqual(cells_of(s), ["w", "x", "y" + AC, "z"])


class RowOutsideTheRegionKeepsTheMemoTest(unittest.TestCase):
    """対照: 範囲の外の行は動かないので、覚えを残すこと。"""

    def test_su_outside_the_region_keeps_the_last_char(self):
        """範囲の外で最終桁へ印字した文字に、そのまま付くこと。"""
        s = feed(Screen(4, 4), NO_WRAP + CSI + "1;2r" + CSI + "4;1H"
                 + "ABcd" + SU + AC)
        self.assertEqual(cells_of(s, 3), ["A", "B", "c", "d" + AC])

    def test_sd_outside_the_region_keeps_the_last_char(self):
        """SD でも同じこと。"""
        s = feed(Screen(4, 4), NO_WRAP + CSI + "1;2r" + CSI + "4;1H"
                 + "ABcd" + SD + AC)
        self.assertEqual(cells_of(s, 3), ["A", "B", "c", "d" + AC])


if __name__ == "__main__":
    unittest.main()
