r"""47 で入った代替画面から 1049l で出たとき、カーソルがメイン画面側の保存位置へ戻ることを検証する。

1049 の入場はカーソルを _saved_main へ保存し、退場はそこから戻す。47 で
入った代替画面では保存が行われず、そのまま 1049h を受けても (すでに
代替画面にいるので) 保存しない。そのため 1049l で出ると _saved_main は
None で、画面はメイン画面へ戻るのにカーソル・属性・文字集合は代替画面で
最後にいたときのままだった。

実測: Screen(3, 10) に 'MAIN' ESC[?47h 'OLD' ESC[?1049h ESC[?1049l を流すと、
画面は ['MAIN', '', ''] に戻るが、カーソルは代替画面での位置 (0, 7) の
まま。代替画面で ESC(0 を受けていると、メイン画面へ戻ったあとの出力も
罫線文字に化けた ('MAIN' ESC 7 ESC[?47h ESC(0 ESC[?1049h ESC[?1049l 'q'
で 'MAIN─')。

直し方: xterm の 1049l は「メイン画面へ戻る → CursorRestore」で、戻す
先はメイン画面の保存領域 (DECSC の保存領域) なので、1049 用の保存が
無いときは、メイン画面側の DECSC の保存領域 (位置・属性・文字集合・
折り返し待ち) から戻す。一度も ESC 7 を受けていなければ原点と既定の
属性へ戻る (xterm も同じ)。1049 で入ったときは、これまでどおり 1049 の
保存から戻す。47l はカーソルを保存・復元しないので動かさない。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
ENTER_47 = ESC + "[?47h"
LEAVE_47 = ESC + "[?47l"
ENTER_1049 = ESC + "[?1049h"
LEAVE_1049 = ESC + "[?1049l"
DECSC = ESC + "7"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class Leave1049After47Test(unittest.TestCase):
    def test_cursor_leaves_the_alternate_screen_position(self):
        """保存が無ければ、メイン画面の保存領域の既定 (原点) へ戻ること。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_47 + "OLD" + ENTER_1049
                 + LEAVE_1049)
        self.assertFalse(s.alt_active)
        self.assertEqual(s.text(), ["MAIN", "", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 0),
                         "カーソルが代替画面での位置のまま")

    def test_cursor_returns_to_the_main_screen_saved_position(self):
        """メイン画面で ESC 7 した位置へ戻り、続きがそこから書かれること。"""
        s = feed(Screen(3, 10), "\r\nMAIN" + DECSC + ESC + "[3;1H" + ENTER_47
                 + ESC + "[2;6H" + "OLD" + ENTER_1049 + LEAVE_1049 + "X")
        self.assertEqual(s.text(), ["", "MAINX", ""])

    def test_charset_of_the_alternate_screen_does_not_leak(self):
        """代替画面で指示した罫線の文字集合を、メイン画面へ持ち帰らないこと。"""
        s = feed(Screen(3, 10), "MAIN" + DECSC + ENTER_47 + ESC + "(0"
                 + ENTER_1049 + LEAVE_1049 + "q")
        self.assertEqual(s.text(), ["MAINq", "", ""],
                         "メイン画面へ戻ったあとも罫線文字に化けている")

    def test_1049_round_trip_still_uses_its_own_save(self):
        """1049 で入ったときは、ESC 7 の位置でなく 1049 の保存から戻すこと。"""
        s = feed(Screen(3, 10), "MAIN" + DECSC + "\r\nNEXT" + ENTER_1049
                 + ESC + "[3;8H" + "OLD" + LEAVE_1049)
        self.assertEqual(s.text(), ["MAIN", "NEXT", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (1, 4))

    def test_47l_does_not_move_the_cursor(self):
        """47 はカーソルを保存・復元しないので、47l では動かないこと。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_47 + "OLD" + LEAVE_47)
        self.assertEqual(s.text(), ["MAIN", "", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 7))


if __name__ == "__main__":
    unittest.main()
