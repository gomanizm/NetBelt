r"""空の画面への消去の連打が、印が落ちた状態ではまだ数秒止まる件を検証する。

Screen._screen_blank (画面が丸ごと空白だという覚え) は、立つのが
__init__ / reset() と ED の全消去 (ESC[2J・原点からの ESC[0J) の直後
だけで、set_size・代替画面の出入り・印字が落とす。ED 1 と原点以外
からの ED 0 は、走査で「画面は丸ごと空白になる」と分かっても印を
立て直さないので、印が落ちた状態からの連打では毎回 blank_before /
blank_after の走査と _record_screen の走査が画面全体へ走っていた。

実測 (基準 429bf80、画面モデルだけ。1 回の受信分 16,384 文字に収まる
繰り返しで、履歴は 1 行も増えない):
  100x300 ESC[1J x4096         作った直後 0.031 秒
                               set_size の直後 5.406 秒 ← 窓・文字の大きさ変更
                               ESC[?1049h の直後 2.673 秒 ← less / vi の入場
                               ESC[?47h の直後 2.623 秒
                               1 文字印字した直後 5.208 秒
  100x300 ESC[100;1H ESC[0J x1365  set_size の直後 1.799 秒
  24x80  ESC[1J x4096          set_size の直後 0.377 秒
TerminalWidget (offscreen) で create_terminal_tab + queue_output +
_flush_pending_output を 1 回流しても同じで、set_size の直後の
ESC[1J x4096 が 100x300 5.092 秒 / 24x80 0.364 秒、ESC[?1049h の直後が
100x300 2.662 秒 (直したあとは 0.039 / 0.016 / 0.035 秒)。
set_size は窓の大きさを変えたときと文字の大きさを変えたときに必ず
呼ばれるので、一度窓を動かすと以降ずっとこの費用になる。

直し方: 走査で「残りは全部空白」と分かった ED 0 / ED 1 のあとも印を
立て直す。ただし折り返しの印が 1 つでも残っているうちは立てない
(全セルが空白でも印が残る状態は実在する。右端の 1 桁で折り返した
あとの空白の行など)。印は「丸ごと空白で、行の長さも桁ちょうど」を
指していたが、ED 0 / ED 1 は作り直さない範囲に窓を狭めた跡の長い行を
残すので、作り直しを省く判断へ行の長さの検査を足して補う。あわせて
全セルの走査を list.count (C 側で回る) に置き換え、印を立てられない
場面 (折り返しの印が残る・中身が下にある) の走査も軽くする。
"""
import sys
import time
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def row_text(line):
    return "".join(cell[0] for cell in line).rstrip()


def spent_after(screen, text):
    """screen へ text を流すのにかかった秒数を返す (解析の時間は含めない)。"""
    events = Parser().feed(text)
    started = time.perf_counter()
    screen.apply(events)
    return time.perf_counter() - started


def blank_line_calls(screen, text):
    """text を流す間に _blank_line が呼ばれた回数を返す。"""
    calls = []
    real = Screen._blank_line

    def counted(self):
        calls.append(1)
        return real(self)

    Screen._blank_line = counted
    try:
        feed(screen, text)
    finally:
        Screen._blank_line = real
    return len(calls)


class EraseAfterTheBlankMarkWasDroppedCostTest(unittest.TestCase):
    def test_a_repeated_erase_above_after_a_resize_stays_fast(self):
        """窓の大きさを変えたあとの ED 1 の連打が、待たされないこと。"""
        screen = Screen(100, 300)
        screen.set_size(100, 301)
        spent = spent_after(screen, (ESC + "[1J") * 4096)
        self.assertEqual(len(screen.history), 0, "前提: 履歴は増えない")
        self.assertLess(spent, 1.0,
                        "大きさを変えたあとの ED 1 の連打が %.3f 秒かかる"
                        % spent)

    def test_a_repeated_erase_below_after_a_resize_stays_fast(self):
        """窓の大きさを変えたあとの ED 0 の連打が、待たされないこと。"""
        screen = Screen(100, 300)
        screen.set_size(100, 301)
        spent = spent_after(
            screen, (ESC + "[100;1H" + ESC + "[0J") * 1365)
        self.assertEqual(len(screen.history), 0, "前提: 履歴は増えない")
        self.assertLess(spent, 1.0,
                        "大きさを変えたあとの ED 0 の連打が %.3f 秒かかる"
                        % spent)

    def test_a_repeated_erase_above_on_the_alternate_screen_stays_fast(self):
        """代替画面へ入ったあとの ED 1 の連打が、待たされないこと。"""
        screen = feed(Screen(100, 300), ESC + "[?1049h")
        spent = spent_after(screen, (ESC + "[1J") * 4096)
        self.assertEqual(len(screen.history), 0, "前提: 履歴は増えない")
        self.assertLess(spent, 1.0,
                        "代替画面での ED 1 の連打が %.3f 秒かかる" % spent)

    def test_a_repeated_erase_above_after_printing_stays_fast(self):
        """1 文字印字して消したあとの ED 1 の連打が、待たされないこと。"""
        screen = feed(Screen(100, 300), "x" + ESC + "[H")
        spent = spent_after(screen, (ESC + "[1J") * 4096)
        self.assertLessEqual(len(screen.history), 1,
                             "前提: 履歴は最初の 1 行しか増えない")
        self.assertLess(spent, 1.0,
                        "印字のあとの ED 1 の連打が %.3f 秒かかる" % spent)

    def test_a_blank_making_erase_above_raises_the_mark_again(self):
        """走査で空と分かった ED 1 のあと、空だという覚えが戻ること。"""
        screen = Screen(4, 8)
        screen.set_size(4, 9)
        self.assertFalse(screen._screen_blank,
                         "前提: 大きさを変えると印が落ちる")
        feed(screen, ESC + "[1J")
        self.assertTrue(screen._screen_blank,
                        "空になった ED 1 のあとも印が落ちたまま")

    def test_a_blank_making_erase_below_raises_the_mark_again(self):
        """原点以外からの ED 0 のあとも、同じく覚えが戻ること。"""
        screen = feed(Screen(4, 8), ESC + "[?1049h")
        self.assertFalse(screen._screen_blank,
                         "前提: 代替画面へ入ると印が落ちる")
        feed(screen, ESC + "[4;1H" + ESC + "[0J")
        self.assertTrue(screen._screen_blank,
                        "空になった ED 0 のあとも印が落ちたまま")


class TheBlankMarkStaysHonestTest(unittest.TestCase):
    def test_a_blank_making_erase_above_still_fits_the_new_width(self):
        """印を立て直しても、あとの ED 2 が新しい桁の行を作ること。"""
        screen = feed(Screen(4, 8), ESC + "[2J")
        screen.set_size(4, 4)
        # ED 1 はカーソル行を _erase_line がその場で払うだけなので、
        # 窓を狭めた跡の長い行がここに残る
        feed(screen, ESC + "[4;4H" + ESC + "[1J")
        feed(screen, ESC + "[2J")
        self.assertEqual([len(line) for line in screen.lines], [4] * 4)

    def test_a_blank_screen_with_a_wrap_mark_is_not_called_blank(self):
        """折り返しの印が残るうちは印を立てず、次の ED 2 が外すこと。"""
        # 右端の桁へ空白を印字して折り返すと、全セルが空白のまま
        # 折り返しの印だけが残る
        screen = feed(Screen(3, 4), ESC + "[2;4H" + "  ")
        self.assertEqual(list(screen.wrapped), [False, True, False],
                         "前提: 空白の行に折り返しの印が付いている")
        feed(screen, ESC + "[1;1H" + ESC + "[1J")
        feed(screen, ESC + "[2J")
        self.assertEqual(list(screen.wrapped), [False] * 3,
                         "空白の行に折り返しの印が残っている")

    def test_printing_after_a_blank_making_erase_is_still_recorded(self):
        """空にした ED 1 のあとに出た行が、次の消去で履歴へ入ること。"""
        screen = feed(Screen(4, 8), "one\r\ntwo")
        feed(screen, ESC + "[1J")           # 画面は丸ごと空白になる
        feed(screen, ESC + "[H" + "three")
        feed(screen, ESC + "[2J")
        self.assertEqual([row_text(line) for line in screen.history],
                         ["one", "two", "three"])

    def test_an_erase_above_that_leaves_content_still_records_later(self):
        """下に中身が残る ED 1 のあとは、次の ED 2 が履歴へ送ること。"""
        screen = feed(Screen(4, 8), "one\r\ntwo\r\nthree")
        feed(screen, ESC + "[1;1H" + ESC + "[1J")
        self.assertEqual(len(screen.history), 0, "前提: まだ履歴へ行かない")
        feed(screen, ESC + "[2J")
        # ED 1 はカーソルのセルまで消すので、1 行目は 1 文字目だけ欠ける
        self.assertEqual([row_text(line) for line in screen.history],
                         [" ne", "two", "three"])


if __name__ == "__main__":
    unittest.main()
