r"""代替画面にいるまま 1049h を受けたとき、代替画面を白紙にすることを検証する。

1049h は「カーソルを保存 → 代替画面へ → 画面を消す」で、xterm は画面を
切り替える必要が無いとき (すでに代替画面にいるとき) も消去は行う。
_switch_screen は切り替え先が今と同じなら何もせずに戻っていたので、
2 回目の 1049h では消去まで届かなかった。

実測: Screen(3, 10) に ESC[?1049h 'OLD' ESC[?1049h を流すと、画面は
['OLD', '', ''] のまま。47 で入った画面から 1049h を受けても同じ。受信の
経路 (TerminalWidget) では、次の出力 NEW が前の中身に続いて '    OLDNEW'
になった。前のアプリが 1049l を出さずに終わったあと (強制終了など) や、
47 を使うアプリのあとに、1049h の白紙化に頼るアプリが来ると混ざる。
代替画面は記録しないので、表示だけの問題。

直し方: すでに代替画面にいるときの 1049h でも、代替画面を白紙にし、
折り返しの印と折り返し待ちを落とす (初めて入るときと同じ)。カーソルは
動かさない。1049 用のカーソル保存 (_saved_main) は上書きしないので、
2 回目の 1049h がメイン画面へ戻るときの位置を潰さない。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
ENTER_1049 = ESC + "[?1049h"
LEAVE_1049 = ESC + "[?1049l"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class ReenterAlternateScreenTest(unittest.TestCase):
    def test_second_1049h_clears_the_alternate_screen(self):
        """代替画面にいるまま 1049h を受けたら、代替画面が白紙になること。"""
        s = feed(Screen(3, 10), ENTER_1049 + "OLD" + ENTER_1049)
        self.assertEqual(s.text(), ["", "", ""],
                         "2 回目の 1049h で前の中身が消えていない")
        self.assertTrue(s.alt_active)
        self.assertEqual(s.wrapped, [False, False, False])

    def test_1049h_after_47h_clears_the_alternate_screen(self):
        """47 で入った代替画面でも、1049h で白紙になること。"""
        s = feed(Screen(3, 10), ESC + "[?47h" + "OLD" + ENTER_1049)
        self.assertEqual(s.text(), ["", "", ""])

    def test_next_output_does_not_mix_with_the_old_content(self):
        """白紙にしたあとの出力が、前の中身に続かないこと (カーソルは動かさない)。"""
        s = feed(Screen(3, 10), ENTER_1049 + ESC + "[1;5H" + "OLD"
                 + ENTER_1049 + "NEW")
        self.assertEqual(s.text(), ["       NEW", "", ""])

    def test_pending_wrap_is_dropped_as_on_first_entry(self):
        """初めて入るときと同じく、右端の折り返し待ちを落とすこと。"""
        s = feed(Screen(3, 4), ENTER_1049 + "ABCD" + ENTER_1049 + "X")
        self.assertEqual(s.text(), ["   X", "", ""])

    def test_leaving_restores_the_main_screen_and_cursor(self):
        """2 回目の 1049h のあとでも、1049l でメイン画面とカーソルが戻ること。"""
        s = feed(Screen(3, 10), "\r\nMAIN" + ENTER_1049 + ESC + "[3;8H"
                 + "OLD" + ENTER_1049 + LEAVE_1049)
        self.assertFalse(s.alt_active)
        self.assertEqual(s.text(), ["", "MAIN", ""])
        self.assertEqual((s.cursor_row, s.cursor_col), (1, 4))

    def test_1047h_while_on_the_alternate_screen_does_not_clear(self):
        """1047 の入場は消さない (XTerm ctlseqs) ので、中身が残ること。"""
        s = feed(Screen(3, 10), ENTER_1049 + "OLD" + ESC + "[?1047h")
        self.assertEqual(s.text(), ["OLD", "", ""])


if __name__ == "__main__":
    unittest.main()
