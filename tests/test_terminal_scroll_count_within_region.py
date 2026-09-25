r"""部分スクロール範囲での SU / DL の回数を、範囲の中の行数で頭打ちにすることを検証する。

SU (ESC[nS) と DL (ESC[nM) の回数は画面の行数で頭打ちにしていたが、範囲の
高さ (DL はカーソルから下端まで) では抑えていなかった。上端が画面の先頭の
部分スクロール範囲で、範囲より大きい回数を受けると、押し出せる元の行を
押し出したあとも、処理中に下端へ足した空行を押し出し続け、存在しなかった
空行が履歴へ入った。

実測: Screen(24, 10) に 'A\r\nB\r\nC' ESC[1;3r ESC[999S を流すと、押し
出せる元の行は 3 行なのに履歴は 24 行 (21 行は処理中に作った空行)。先頭の
DL (ESC[H ESC[999M) も 24 行、SU 4 でも 4 行 (空行が 1 行余分)。999S を
3 回送ると 72 行になり、max_history=50 の画面では先頭の 'old1' が押し
出された。受信の経路 (TerminalWidget) でも文書に同じだけ空行が入った。

直し方: xterm と同じく、SU の回数は範囲の高さ、DL (と IL) の回数は
カーソルから範囲の下端までの行数で頭打ちにする。全画面の範囲の 999S は
従来どおり画面の行数ぶん (xterm が全画面を巻き上げたときと同じ)。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
THREE_ROWS_IN_REGION = "A\r\nB\r\nC" + ESC + "[1;3r"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def history_text(screen):
    return ["".join(cell[0] for cell in line).rstrip()
            for line in screen.history]


class ScrollCountWithinRegionTest(unittest.TestCase):
    def test_scroll_up_past_the_region_height(self):
        """範囲の高さを超える SU でも、履歴には範囲の行だけが入ること。"""
        s = feed(Screen(24, 10), THREE_ROWS_IN_REGION + ESC + "[999S")
        self.assertEqual(history_text(s), ["A", "B", "C"],
                         "存在しなかった空行が履歴へ入っている")
        self.assertEqual(len(s.take_new_history()), 3)
        self.assertEqual(s.text()[:3], ["", "", ""])

    def test_scroll_up_by_one_more_than_the_region(self):
        """範囲より 1 行多い SU でも、余分な空行が入らないこと。"""
        s = feed(Screen(24, 10), THREE_ROWS_IN_REGION + ESC + "[4S")
        self.assertEqual(history_text(s), ["A", "B", "C"])

    def test_delete_lines_from_the_top_past_the_region(self):
        """範囲の先頭からの DL も、範囲の下端までの行数で止まること。"""
        s = feed(Screen(24, 10),
                 THREE_ROWS_IN_REGION + ESC + "[H" + ESC + "[999M")
        self.assertEqual(history_text(s), ["A", "B", "C"])
        self.assertEqual(s.text()[:3], ["", "", ""])

    def test_rows_below_the_region_are_left_alone(self):
        """範囲の外の行はそのまま残ること。"""
        s = feed(Screen(24, 10), "A\r\nB\r\nC\r\nD" + ESC + "[1;3r"
                 + ESC + "[999S")
        self.assertEqual(s.text()[:4], ["", "", "", "D"])

    def test_repeated_scrolls_keep_older_history(self):
        """繰り返しても空行が保持上限を食い潰して古い記録を押し出さないこと。"""
        s = feed(Screen(24, 10, max_history=50),
                 "old1" + ESC + "[1;3r" + (ESC + "[999S") * 3)
        self.assertEqual(len(s.history), 9)
        self.assertEqual(history_text(s)[0], "old1")

    def test_full_screen_scroll_still_pushes_every_row(self):
        """全画面の範囲の 999S は従来どおり画面の行数ぶん押し出すこと。"""
        s = feed(Screen(24, 10), "A\r\nB\r\nC" + ESC + "[999S")
        self.assertEqual(len(s.history), 24)
        self.assertEqual(history_text(s)[:4], ["A", "B", "C", ""])


if __name__ == "__main__":
    unittest.main()
