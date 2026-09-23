r"""先にメイン画面へ戻ったあとの 1049l が、何もせず抜ける件を検証する。

_switch_screen は先頭で `to_alt == self.alt_active` なら早期 return して
いた。1049h で代替画面へ入ったあと 47l (または 1047l) で先にメイン画面へ
戻ると alt_active が False になり、続く 1049l はこの早期 return で何も
せずに抜ける。切り替えは済んでいるのに 1049h の保存 (_saved_main) からの
復元が行われないので、カーソル位置も属性も文字集合も代替画面のまま残る。
_saved_main は 47l では捨てられない (捨てるのは 47h / 1047h の入場だけ)
ので、戻す材料は残っている。

実測 (基準 aa38a2b、Screen(3, 10)):
  'MAIN' ESC[?1049h ESC[3;2H ESC(0 ESC[?47l ESC[?1049l 'q'
    HEAD : ['MAIN', '', ' ─']  cursor (2, 2)  G0='0' のまま
    正   : ['MAINq', '', '']   cursor (0, 5)  G0='B'
  ESC[?1047l 版も同じ。47l を挟まない素の 1049h / 1049l は正しい。
  2 回目の 1049l も同じ穴で、
  'MAIN' ESC[?1049h ESC[3;2H ESC[?1049l ESC[3;5H ESC[?1049l 'q' は
  ['MAIN', '', '    q'] になる (xterm は CursorRestore するので 'MAINq')。
  実害は文字集合の居座りで、以降のメイン画面の出力とログが罫線文字へ
  化け続ける。位置のずれのほうは次の出力がプロンプト行を潰す。

利用者の決定 (2026-09-23): 保存があるときだけ戻す。1049h の保存
(_saved_main) が残っているときだけ、位置・属性・文字集合・SI/SO を戻し、
使った保存は捨てる (同じ保存を使い回さない)。1049h を一度も受けていない
迷子の 1049l (tput rmcup など) は、これまでどおり何もしない。無条件に
戻すと 'PROMPT' ESC[?1049l 'X' が ['XROMPT'] になり、1049h を送らない
機器の記録を潰すため。

直し方: 早期 return の中に「メイン画面へ戻る向きの 1049l で _saved_main
が残っていれば、そこから復元して捨てる」枝を足した。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
ENTER_47 = ESC + "[?47h"
LEAVE_47 = ESC + "[?47l"
LEAVE_1047 = ESC + "[?1047l"
ENTER_1049 = ESC + "[?1049h"
LEAVE_1049 = ESC + "[?1049l"
DEC_GRAPHICS = ESC + "(0"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class Leave1049AfterLeavingTheAlternateScreenTest(unittest.TestCase):
    def test_47l_then_1049l_restores_the_main_screen_cursor(self):
        """47l で戻ったあとの 1049l でも、1049h の保存から戻すこと。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_1049 + ESC + "[3;2H"
                 + LEAVE_47 + LEAVE_1049 + "q")
        self.assertEqual(s.text(), ["MAINq", "", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 5))

    def test_47l_then_1049l_restores_the_charset(self):
        """代替画面で指示した罫線の文字集合を、持ち帰らないこと。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_1049 + ESC + "[3;2H"
                 + DEC_GRAPHICS + LEAVE_47 + LEAVE_1049 + "q")
        self.assertEqual(s.text(), ["MAINq", "", ""],
                         "メイン画面へ戻ったあとも罫線文字に化けている")
        self.assertEqual(s._g["("], "B")

    def test_1047l_then_1049l_restores_the_main_screen_cursor(self):
        """1047l で戻ったあとの 1049l でも同じこと。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_1049 + ESC + "[3;2H"
                 + DEC_GRAPHICS + LEAVE_1047 + LEAVE_1049 + "q")
        self.assertEqual(s.text(), ["MAINq", "", ""])

    def test_a_second_1049l_restores_from_the_same_save(self):
        """切り替えの済んだ 2 回目の 1049l も、保存から戻すこと。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_1049 + ESC + "[3;2H"
                 + LEAVE_1049 + ESC + "[3;5H" + LEAVE_1049 + "q")
        self.assertEqual(s.text(), ["MAINq", "", ""])

    def test_the_used_save_is_not_reused(self):
        """使った保存は捨てて、次の 1049l では何もしないこと。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_1049 + ESC + "[3;2H"
                 + LEAVE_47 + LEAVE_1049 + ESC + "[3;5H" + LEAVE_1049 + "q")
        self.assertEqual(s.text(), ["MAIN", "", "    q"])
        self.assertIsNone(s._saved_main)


class Stray1049lDoesNothingTest(unittest.TestCase):
    """1049h を一度も受けていない 1049l は、これまでどおり動かないこと。"""

    def test_a_stray_1049l_keeps_the_line(self):
        """rmcup だけを送る機器の行を潰さないこと。"""
        s = feed(Screen(3, 10), "PROMPT" + LEAVE_1049 + "X")
        self.assertEqual(s.text(), ["PROMPTX", "", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 7))

    def test_47h_discards_the_save_so_1049l_does_nothing(self):
        """47h の入場で保存を捨てたあとは、戻す材料が無いこと。"""
        s = feed(Screen(3, 10), "MAIN" + ENTER_1049 + LEAVE_1049
                 + ENTER_47 + ESC + "[3;2H" + LEAVE_47 + LEAVE_1049 + "q")
        self.assertEqual(s.text(), ["MAIN", "", " q"])


if __name__ == "__main__":
    unittest.main()
