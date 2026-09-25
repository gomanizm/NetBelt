r"""DECSET/DECRST の ?1048 が、DECSC / DECRC と同じ保存・復元をすることを検証する。

_private_mode は 47 / 1047 / 1049 / 7 / 25 / 1 / 2004 しか見ておらず、
?1048 を黙って捨てていた。?1048 は xterm では「カーソルの保存・復元だけ
を行うモード」で、画面の切り替えは伴わない (1049 = 1048 + 1047)。ESC 7 /
ESC 8 を送らず ESC[?1048h / ESC[?1048l だけでカーソルを退避するアプリ
では、復元されずに位置・属性・文字集合がずれたままになる。

実測 (基準 8b0c94e): Screen(3, 10) に 'ABC' ESC[?1048h ESC[3;1H 'Z'
ESC[?1048l を流すと、カーソルは (2, 1) のまま (xterm は保存した (0, 3)
へ戻す)。属性と文字集合も戻らないので、代替画面を使わないアプリでも
ESC(0 のまま罫線文字に化け続けた。

直し方: mode == 1048 の枝を足し、ESC 7 / ESC 8 と同じ保存領域
(self._saved) を使って保存・復元する。1049 は 1048 + 1047 なので、
1049 用の _saved_main ではなく _saved を使うのが xterm と揃う。
保存・復元の中身は DECSC / DECRC と共通の処理へまとめた。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.attrs import DEFAULT     # noqa: E402
from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
SAVE_1048 = ESC + "[?1048h"
RESTORE_1048 = ESC + "[?1048l"
DECSC = ESC + "7"
DECRC = ESC + "8"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class PrivateMode1048Test(unittest.TestCase):
    def test_1048_restores_the_cursor_position(self):
        """?1048h で保存した位置へ ?1048l が戻すこと。"""
        s = feed(Screen(3, 10), "ABC" + SAVE_1048 + ESC + "[3;1H" + "Z"
                 + RESTORE_1048 + "X")
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 4))
        self.assertEqual(s.text(), ["ABCX", "", "Z"])

    def test_1048_does_not_switch_screens(self):
        """?1048 は画面を切り替えず、中身も消さないこと。"""
        s = feed(Screen(3, 10), "ABC" + SAVE_1048)
        self.assertFalse(s.alt_active)
        self.assertEqual(s.text(), ["ABC", "", ""])
        feed(s, RESTORE_1048)
        self.assertFalse(s.alt_active)
        self.assertEqual(s.text(), ["ABC", "", ""])

    def test_1048_restores_the_attributes(self):
        """属性も DECSC / DECRC と同じく戻すこと。"""
        s = feed(Screen(3, 10), ESC + "[1m" + SAVE_1048 + ESC + "[0m")
        self.assertEqual(s.attr, DEFAULT)
        feed(s, RESTORE_1048)
        self.assertTrue(s.attr.bold, "保存した属性が戻っていない")

    def test_1048_restores_the_charset(self):
        """文字集合の指示も戻すこと。"""
        s = feed(Screen(3, 10), "ABC" + SAVE_1048 + ESC + "(0"
                 + RESTORE_1048 + "q")
        self.assertEqual(s.text(), ["ABCq", "", ""],
                         "罫線用の文字集合が戻っていない")

    def test_1048_shares_the_save_area_with_decsc(self):
        """保存領域は ESC 7 / ESC 8 と共通であること。"""
        s = feed(Screen(3, 10), "ABC" + DECSC + ESC + "[3;1H"
                 + RESTORE_1048)
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 3),
                         "ESC 7 の保存から戻っていない")
        s = feed(Screen(3, 10), "ABC" + SAVE_1048 + ESC + "[3;1H" + DECRC)
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 3),
                         "?1048h の保存から戻っていない")

    def test_decsc_and_decrc_still_work(self):
        """ESC 7 / ESC 8 がこれまでどおり動くこと。"""
        s = feed(Screen(3, 10), "ABC" + DECSC + ESC + "[3;1H" + "Z" + DECRC
                 + "X")
        self.assertEqual(s.text(), ["ABCX", "", "Z"])
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 4))


if __name__ == "__main__":
    unittest.main()
