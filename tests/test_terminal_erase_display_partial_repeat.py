r"""ED 2 以外の消去でも、空の画面の連打で GUI が数秒止まる件を検証する。

ED 2 の連打 (tests/test_terminal_erase_display_repeat.py) は
Screen._screen_blank で直したが、同じ症状が隣の枝から残っていた。
_erase_display の blank_before / blank_after は「履歴へ送るか」を決める
ために画面全体を 1 セルずつ走査するのに、_screen_blank で守られて
いない。_switch_screen も、画面を入れ替えない呼び出し (すでにその画面
に居るとき) で印を落としてしまい、そのあとの走査が丸ごと走る。

実測 (基準 334cd72、画面モデルだけ。いずれも履歴は 1 行も増えない):
  ESC[1J x4096            100x300  2.905 秒 / 200x500  9.561 秒
  ESC[<rows>;1H ESC[0J x1365       100x300 0.956 秒 / 200x500 3.267 秒
  ESC c (RIS) x4096       100x300  3.065 秒
  ESC[?1049l (すでにメイン画面) + ESC[2J x2048  100x300 1.447 秒
RIS と 1049l は _switch_screen が印を落とすため、続く _record_screen が
画面全体を走査していた。TerminalWidget (offscreen) で queue_output +
_flush_pending_output を 1 回流すと ESC[1J x4096 が 24x80 0.219 秒 /
100x300 2.973 秒、ESC c x4096 が 100x300 3.037 秒 (直したあとは
0.019 / 0.040 / 0.261 秒)。

直し方: blank_before / blank_after を _screen_blank で短絡する (印が
立っていれば全セルが BLANK なので any(...) は必ず False で、結果は
変わらない)。_switch_screen の印を落とす行は、画面を入れ替えない
早期 return の後ろへ移す (入れ替えない呼び出しは画面を白紙にする
だけで、中身を増やさないため)。

このファイルの時間を見るテストは、他のテストと同時に流すと混雑だけで
落ちることがある。落ちたらこのファイルだけを単独で流し直して判断する。
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


def spent_on(rows, cols, text):
    """text を流すのにかかった秒数を返す (解析の時間は含めない)。"""
    screen = Screen(rows, cols)
    events = Parser().feed(text)
    started = time.perf_counter()
    screen.apply(events)
    return time.perf_counter() - started, screen


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


class ErasePartialRepeatCostTest(unittest.TestCase):
    def test_a_repeated_erase_above_stays_fast_on_a_large_screen(self):
        """空の画面への ED 1 の連打が、待たされる時間にならないこと。"""
        spent, screen = spent_on(100, 300, (ESC + "[1J") * 4096)
        self.assertEqual(len(screen.history), 0, "前提: 履歴は増えない")
        self.assertLess(spent, 1.0, "ED 1 の連打が %.3f 秒かかる" % spent)

    def test_a_repeated_erase_below_stays_fast_on_a_large_screen(self):
        """空の画面への ED 0 の連打が、待たされる時間にならないこと。"""
        spent, screen = spent_on(
            200, 500, (ESC + "[200;1H" + ESC + "[0J") * 1365)
        self.assertEqual(len(screen.history), 0, "前提: 履歴は増えない")
        self.assertLess(spent, 1.0, "ED 0 の連打が %.3f 秒かかる" % spent)

    def test_a_repeated_reset_stays_fast_on_a_large_screen(self):
        """空の画面への RIS の連打が、待たされる時間にならないこと。"""
        spent, screen = spent_on(100, 300, (ESC + "c") * 4096)
        self.assertEqual(len(screen.history), 0, "前提: 履歴は増えない")
        self.assertLess(spent, 1.0, "RIS の連打が %.3f 秒かかる" % spent)

    def test_a_screen_switch_that_does_not_switch_keeps_the_blank_mark(self):
        """入れ替えない 1049l のあとの ED 2 が、画面を作り直さないこと。"""
        screen = Screen(100, 300)
        calls = blank_line_calls(
            screen, (ESC + "[?1049l" + ESC + "[2J") * 64)
        self.assertLessEqual(calls, screen.rows,
                             "空の画面を何度も作り直している")


class ErasePartialRepeatBehaviourTest(unittest.TestCase):
    def test_an_erase_above_still_sends_the_screen_to_the_history(self):
        """下に中身が残らない ED 1 が、これまでどおり履歴へ送ること。"""
        screen = feed(Screen(4, 8), "one\r\ntwo")
        feed(screen, ESC + "[1J")
        self.assertEqual([row_text(line) for line in screen.history],
                         ["one", "two"])
        self.assertEqual(screen.text(), [""] * 4)

    def test_an_erase_above_keeps_the_history_when_content_is_below(self):
        """下に中身が残る ED 1 は、これまでどおり履歴へ送らないこと。"""
        screen = feed(Screen(4, 8), "one\r\ntwo\r\nthree")
        feed(screen, ESC + "[1;1H" + ESC + "[1J")
        self.assertEqual(len(screen.history), 0)
        self.assertEqual(screen.text()[1:3], ["two", "three"])

    def test_an_erase_below_from_the_home_position_still_records(self):
        """原点からの ED 0 が、これまでどおり履歴へ送ること。"""
        screen = feed(Screen(4, 8), "one\r\ntwo")
        feed(screen, ESC + "[H" + ESC + "[0J")
        self.assertEqual([row_text(line) for line in screen.history],
                         ["one", "two"])

    def test_a_reset_still_sends_the_screen_to_the_history(self):
        """中身のある画面の RIS が、これまでどおり履歴へ送ること。"""
        screen = feed(Screen(4, 8), "one\r\ntwo")
        feed(screen, ESC + "c")
        self.assertEqual([row_text(line) for line in screen.history],
                         ["one", "two"])
        self.assertEqual(screen.text(), [""] * 4)

    def test_a_second_1049h_still_clears_the_alternate_screen(self):
        """代替画面に居るままの 1049h が、これまでどおり消すこと。"""
        screen = feed(Screen(4, 8), "kept")
        feed(screen, ESC + "[?1049h" + "alt")
        feed(screen, ESC + "[?1049h")
        self.assertEqual(screen.text(), [""] * 4)
        feed(screen, ESC + "[?1049l")
        self.assertEqual(screen.text()[0], "kept")

    def test_an_erase_after_a_noop_switch_still_records(self):
        """入れ替えない 1049l のあとでも、中身が履歴へ落ちること。"""
        screen = feed(Screen(4, 8), "one\r\ntwo")
        feed(screen, ESC + "[?1049l" + ESC + "[2J")
        self.assertEqual([row_text(line) for line in screen.history],
                         ["one", "two"])

    def test_an_erase_above_after_a_resize_still_fits_the_new_width(self):
        """大きさを変えたあとの ED 1 が、新しい桁の行を作ること。"""
        screen = feed(Screen(4, 8), ESC + "[2J")
        screen.set_size(4, 4)
        feed(screen, ESC + "[4;4H" + ESC + "[1J")
        # 作り直すのはカーソル行より上。カーソル行は _erase_line が
        # その場で払うので、長いまま残る (窓を狭めても切らないため)
        self.assertEqual([len(line) for line in screen.lines[:3]], [4] * 3)


if __name__ == "__main__":
    unittest.main()
