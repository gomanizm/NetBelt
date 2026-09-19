r"""保存から戻した折り返し待ちが、折り返し無効 (ESC[?7l) の間は折り返さないことを検証する。

DECSC (ESC 7) と 1049 の入場は、xterm と同じく右端の折り返し待ちも保存し、
DECRC (ESC 8) と 1049 の退場で戻す。ところが印字側 (_print_narrow と
_print_chars) は、折り返し待ちを実行するときに折り返しが有効かを見て
いなかった。そのため右端ちょうどで保存し、ESC[?7l のまま戻して印字すると、
折り返しを切っているのに次の行へ流れた。ESC[?7l を受けたときは折り返し
待ちを捨てているので、戻す経路だけがこの方針から漏れていた。

実測: Screen(2, 4) に 'ABCD' ESC 7 ESC[?7l ESC 8 'X' を流すと、画面は
['ABCD', 'X'] で wrapped は [True, False] (期待は ['ABCX', '']、印なし)。
全角 (1 文字ずつの経路) でも、1049 の退場で戻した場合も同じだった。
wrapped[0] が残るので、履歴・コピーでは 'ABCD' と 'X' が 1 行に繋がる。

直し方: xterm の印字処理と同じく、折り返し待ちを実行する時点で折り返しが
無効なら、待ちを捨てて右端へ重ねて書く。確かめるのは実行する時点なので、
戻したあとに ESC[?7h へ戻してから印字すれば、xterm と同じく折り返す。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
KANJI = chr(0x6F22)
SAVE_AT_EDGE_THEN_NOWRAP = "ABCD" + ESC + "7" + ESC + "[?7l" + ESC + "8"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class RestoredPendingWrapTest(unittest.TestCase):
    def test_decrc_does_not_wrap_while_autowrap_is_off(self):
        """DECRC で戻した折り返し待ちでも、?7l の間は右端へ重ねること。"""
        s = feed(Screen(2, 4), SAVE_AT_EDGE_THEN_NOWRAP + "X")
        self.assertEqual(s.text(), ["ABCX", ""])
        self.assertEqual(s.wrapped, [False, False],
                         "折り返していないのに折り返しの印が付いている")
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 3))

    def test_leaving_1049_does_not_wrap_while_autowrap_is_off(self):
        """1049 の退場で戻した折り返し待ちでも、?7l の間は右端へ重ねること。"""
        s = feed(Screen(2, 4), "ABCD" + ESC + "[?1049h" + ESC + "[?7l"
                 + ESC + "[?1049l" + "X")
        self.assertEqual(s.text(), ["ABCX", ""])
        self.assertEqual(s.wrapped, [False, False])

    def test_wide_char_does_not_wrap_while_autowrap_is_off(self):
        """全角 (1 文字ずつの経路) でも、折り返さずに右端の手前へ重ねること。"""
        s = feed(Screen(2, 4), SAVE_AT_EDGE_THEN_NOWRAP + KANJI)
        self.assertEqual(s.text(), ["AB" + KANJI, ""])
        self.assertEqual(s.wrapped, [False, False])

    def test_line_drawing_does_not_wrap_while_autowrap_is_off(self):
        """罫線 (1 文字ずつの経路) でも、折り返さずに右端へ重ねること。"""
        s = feed(Screen(2, 4), SAVE_AT_EDGE_THEN_NOWRAP + ESC + "(0q")
        self.assertEqual(s.text(), ["ABC" + chr(0x2500), ""])
        self.assertEqual(s.wrapped, [False, False])

    def test_autowrap_turned_back_on_before_printing_wraps(self):
        """戻したあと ?7h へ戻してから印字すれば、xterm と同じく折り返すこと。"""
        s = feed(Screen(2, 4), SAVE_AT_EDGE_THEN_NOWRAP + ESC + "[?7h" + "X")
        self.assertEqual(s.text(), ["ABCD", "X"])
        self.assertEqual(s.wrapped, [True, False])

    def test_the_rows_are_not_joined_in_history(self):
        """押し出した履歴で、重ねて書いた行が次の行と繋がらないこと。"""
        s = feed(Screen(2, 4), SAVE_AT_EDGE_THEN_NOWRAP + "X\r\nnext\r\n")
        history = [("".join(c[0] for c in line).rstrip(), wrapped)
                   for line, wrapped in s.take_new_history()]
        self.assertEqual(history, [("ABCX", False)])


if __name__ == "__main__":
    unittest.main()
