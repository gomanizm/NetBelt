r"""1049 を通ったあとに 47 で入り直したとき、古い 1049 の保存へ戻さないことを検証する。

1049 用の保存 (_saved_main) を None へ戻すのは reset() だけだった。
そのため 1049 の往復を一度でも通ったあとに 47h / 1047h で代替画面へ入り、
1049l で出ると、そのときの保存ではなく「前の 1049 で保存した位置・属性・
文字集合」へ戻ってしまう。47 の入場では保存しないので、本来はメイン画面
側の DECSC の保存領域 (ESC 7) から戻すのが xterm の動き。

実測 (基準 8b0c94e): Screen(3, 10) に 'MAIN' ESC[?1049h ESC[?1049l
ESC[2;1H 'SECOND' ESC 7 ESC[?47h ESC[3;9H 'O' ESC[?1049l 'X' を流すと
['MAINX', 'SECOND', '']・カーソル (0, 5) (期待は ['MAIN', 'SECONDX', '']
・(1, 7))。文字集合でも同じで、ESC(0 のまま 1049 を往復したあとに ESC(B
ESC 7 ESC[?47h ESC[?1049l 'q' とすると 'MAIN─' に化けた ('MAINq' が期待)。
set_size も alt_active のとき _saved_main[0] を残す行に使うので、47 で
入った代替画面で窓を縮めると、Screen(4, 10) の 'A' ESC[4;1H で 1049 を
往復 → ESC[1;1H ESC[?47h → set_size(2, 10) で、下の空行を捨てれば足りる
のに 'A' の行を履歴へ押し出していた。

直し方: _switch_screen で、1049 用の保存をしない入場 (47h / 1047h) の
ときは古い _saved_main を捨てる。1049 の往復と 47h→47l はこれまでどおり。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
ENTER_47 = ESC + "[?47h"
LEAVE_47 = ESC + "[?47l"
ENTER_1047 = ESC + "[?1047h"
ENTER_1049 = ESC + "[?1049h"
LEAVE_1049 = ESC + "[?1049l"
DECSC = ESC + "7"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class Stale1049SaveAfter47Test(unittest.TestCase):
    def test_47_entry_does_not_restore_the_old_1049_position(self):
        """1049 を往復したあと 47 で入っても、古い保存へ戻さないこと。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_1049 + LEAVE_1049
                 + ESC + "[2;1H" + "SECOND" + DECSC + ENTER_47
                 + ESC + "[3;9H" + "O" + LEAVE_1049 + "X")
        self.assertEqual(s.text(), ["MAIN", "SECONDX", ""],
                         "古い 1049 の保存位置へ戻っている")
        self.assertEqual((s.cursor_row, s.cursor_col), (1, 7))

    def test_47_entry_does_not_restore_the_old_1049_charset(self):
        """古い 1049 の保存に入っていた文字集合を持ち帰らないこと。"""
        s = feed(Screen(3, 10), "MAIN" + ESC + "(0" + ENTER_1049 + LEAVE_1049
                 + ESC + "(B" + DECSC + ENTER_47 + LEAVE_1049 + "q")
        self.assertEqual(s.text(), ["MAINq", "", ""],
                         "メイン画面へ戻ったあとの出力が罫線文字に化けている")

    def test_1047_entry_also_drops_the_old_1049_save(self):
        """1047 の入場でも、古い 1049 の保存を捨てること。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_1049 + LEAVE_1049
                 + ESC + "[2;1H" + "SECOND" + DECSC + ENTER_1047
                 + ESC + "[3;9H" + "O" + LEAVE_1049 + "X")
        self.assertEqual(s.text(), ["MAIN", "SECONDX", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (1, 7))

    def test_resizing_in_a_47_screen_does_not_use_the_old_save(self):
        """47 の代替画面で窓を縮めても、古い保存の行を守らないこと。"""
        s = feed(Screen(4, 10), "A" + ESC + "[4;1H" + ENTER_1049 + LEAVE_1049
                 + ESC + "[1;1H" + ENTER_47)
        s.set_size(2, 10)
        main = ["".join(cell[0] for cell in line).rstrip()
                for line in s._other]
        self.assertEqual(main, ["A", ""],
                         "捨てられる空行でなく、書かれた行が押し出された")
        self.assertEqual(len(s.history), 0, "書かれた行が履歴へ送られている")

    def test_1049_round_trip_still_restores_its_own_save(self):
        """1049 の往復は、これまでどおり 1049 の保存から戻すこと。"""
        s = feed(Screen(3, 10), "MAIN" + DECSC + "\r\nNEXT" + ENTER_1049
                 + ESC + "[3;8H" + "OLD" + LEAVE_1049 + "X")
        self.assertEqual(s.text(), ["MAIN", "NEXTX", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (1, 5))

    def test_47_round_trip_still_leaves_the_cursor_alone(self):
        """47h→47l はカーソルを保存・復元しないままであること。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_1049 + LEAVE_1049
                 + ENTER_47 + "OLD" + LEAVE_47)
        self.assertEqual(s.text(), ["MAIN", "", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 7))


if __name__ == "__main__":
    unittest.main()
