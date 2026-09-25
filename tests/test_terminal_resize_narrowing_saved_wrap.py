r"""窓を狭めたあと、保存領域から戻した折り返し待ちが桁を消す件を検証する。

set_size は「狭めたら折り返し待ちを解く」ようにした (利用者の決定、
2026-09-20)。生きているカーソルの待ちはこれで解けるが、待ちは保存領域
にも入っていて、そこは狭めても解かれないまま残っていた。

実測 (基準 334cd72):
  Screen(3,8) へ '01234567' → ESC[?1049h → set_size(3,4) → ESC[?1049l
  → 'A' が ['0123', 'A', ''] (受信済みの 4 桁が消える。正は
  ['012A4567', '', ''])
  '01234567' + ESC 7 → set_size(3,4) → ESC 8 + 'A' も同じ
  ESC[?1048h / ESC[?1048l (DECSC と同じ保存領域) も同じ

窓を狭めるのが代替画面のアプリ (less / vi 等) の中で起きたときや、
機器が ESC 7 で位置を控えている最中に窓を狭めたときに当たる。戻した
待ちのまま次の 1 文字を書くと _linefeed(from_wrap=True) の
del self.lines[row][self.cols:] が新しい桁で行を切り、右端の外にあった
旧桁 - 新桁 文字が画面・文書・コピー・全ログ保存から消える。基準
81664d2 でも同じ結果で、この周の退行ではなく同じ不具合の別入口。

直し方: set_size で狭めたときは、保存領域 (_saved / _other_saved /
_saved_main) の折り返し待ちも一緒に解く。解く理由は生きている
カーソルのときと同じで、位置・属性・文字集合は触らない。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def text_of(screen):
    return ["".join(cell[0] for cell in line).rstrip()
            for line in screen.lines]


class NarrowingReleasesTheSavedPendingWrapTest(unittest.TestCase):
    def test_leaving_the_alternate_screen_after_narrowing_keeps_columns(self):
        """代替画面の中で狭めても、持ち帰った待ちが桁を消さないこと。"""
        screen = feed(Screen(3, 8), "01234567")
        feed(screen, ESC + "[?1049h")
        screen.set_size(3, 4)
        feed(screen, ESC + "[?1049l" + "A")
        self.assertEqual(text_of(screen), ["012A4567", "", ""])

    def test_a_restored_cursor_after_narrowing_keeps_columns(self):
        """ESC 7 のあとに狭めても、ESC 8 で戻した待ちが桁を消さないこと。"""
        screen = feed(Screen(3, 8), "01234567" + ESC + "7")
        screen.set_size(3, 4)
        feed(screen, ESC + "8" + "A")
        self.assertEqual(text_of(screen), ["012A4567", "", ""])

    def test_a_restored_cursor_from_1048_after_narrowing_keeps_columns(self):
        """?1048 の保存・復元も同じ保存領域なので、同じく消さないこと。"""
        screen = feed(Screen(3, 8), "01234567" + ESC + "[?1048h")
        screen.set_size(3, 4)
        feed(screen, ESC + "[?1048l" + "A")
        self.assertEqual(text_of(screen), ["012A4567", "", ""])

    def test_a_save_made_on_the_alternate_screen_is_released_too(self):
        """裏へ退避した保存領域の待ちも、狭めたときに解けること。"""
        # メイン画面で ESC 7 したあと代替画面へ行き、そこで狭める。
        # メイン画面の保存領域は裏 (_other_saved) にある
        screen = feed(Screen(3, 8), "01234567" + ESC + "7")
        feed(screen, ESC + "[?47h")
        screen.set_size(3, 4)
        feed(screen, ESC + "[?47l" + ESC + "8" + "A")
        self.assertEqual(text_of(screen), ["012A4567", "", ""])


class NarrowingKeepsTheRestOfTheSaveTest(unittest.TestCase):
    def test_leaving_the_alternate_screen_without_a_resize_still_wraps(self):
        """大きさを変えなければ、持ち帰った待ちはこれまでどおり折り返す。"""
        screen = feed(Screen(3, 8), "01234567")
        feed(screen, ESC + "[?1049h" + ESC + "[?1049l" + "A")
        self.assertEqual(text_of(screen), ["01234567", "A", ""])

    def test_a_restored_cursor_without_a_resize_still_wraps(self):
        """大きさを変えなければ、ESC 8 の待ちもこれまでどおり折り返す。"""
        screen = feed(Screen(3, 8), "01234567" + ESC + "7" + ESC + "8" + "A")
        self.assertEqual(text_of(screen), ["01234567", "A", ""])

    def test_a_restored_cursor_after_widening_still_wraps(self):
        """桁を広げた側は変えない (狭めたときだけ解く)。"""
        screen = feed(Screen(3, 4), "0123" + ESC + "7")
        screen.set_size(3, 8)
        feed(screen, ESC + "8" + "A")
        self.assertEqual(text_of(screen), ["0123", "A", ""])

    def test_narrowing_keeps_the_saved_attributes_and_charset(self):
        """解くのは待ちだけ。属性・文字集合・位置はそのまま戻すこと。"""
        screen = feed(Screen(3, 8), ESC + "[31m" + ESC + "(0" + "0123" +
                      ESC + "7")
        screen.set_size(3, 4)
        feed(screen, ESC + "[0m" + ESC + "(B" + ESC + "8")
        self.assertEqual(screen.attr.fg, 1)
        self.assertEqual(screen._g[screen._charset], "0")
        self.assertEqual((screen.cursor_row, screen.cursor_col), (0, 3))


if __name__ == "__main__":
    unittest.main()
