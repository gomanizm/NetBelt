r"""DECSTR (CSI ! p、ソフトリセット) で挿入モード (IRM) が解除されることを検証する。

_csi は中間バイト付きの CSI をまとめて捨てていたので、DECSTR も何も
起こさなかった。IRM (ESC[4h / ESC[4l) を実装する前は表示に効くモードが
無く問題にならなかったが、IRM が効くようになってからは、挿入モードの
まま DECSTR を受けると以後の印字が上書きにならず右の文字を送り続けた。

実測: Screen(2, 6) に ESC[4h ESC[!p 'abcd' ESC[1;1H 'X' を流すと、画面は
'Xabcd' (期待は 'Xbcd')。DECSTR で端末を既定へ戻してから描き直すアプリ
では、表示と相手が思っている行が食い違う。

直し方: DECSTR で insert_mode を False に戻す (DEC の既定値)。ほかの
モードは以前から DECSTR で戻しておらず、範囲を広げると既存の振る舞い
まで変わるので、IRM に絞る。画面の中身とカーソルには触らない (DECSTR
は消去しない)。同じ最終文字 p でも中間バイトが違う列 (DECRQM の
CSI Ps $ p など) は、これまでどおり捨てる。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
IRM_ON = ESC + "[4h"
DECSTR = ESC + "[!p"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class SoftResetInsertModeTest(unittest.TestCase):
    def test_decstr_turns_insert_mode_off(self):
        """DECSTR のあとの印字は、挿入でなく上書きになること。"""
        s = feed(Screen(2, 6), IRM_ON + DECSTR + "abcd" + ESC + "[1;1H" + "X")
        self.assertEqual(s.text(), ["Xbcd", ""],
                         "DECSTR のあとも挿入モードのまま")
        self.assertFalse(s.insert_mode)

    def test_decstr_keeps_the_screen_and_the_cursor(self):
        """DECSTR は画面を消さず、カーソルも動かさないこと。"""
        s = feed(Screen(2, 6), "abc" + DECSTR + "d")
        self.assertEqual(s.text(), ["abcd", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 4))

    def test_other_intermediates_with_p_are_still_ignored(self):
        """中間バイトが違う CSI ... p (DECRQM) では、挿入モードのままのこと。"""
        s = feed(Screen(2, 6), IRM_ON + ESC + "[4$p")
        self.assertTrue(s.insert_mode)


if __name__ == "__main__":
    unittest.main()
