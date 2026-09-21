r"""画面消去 (ED 2) の連打で GUI が数秒〜十数秒止まる件を検証する。

1 回の受信分 (OUTPUT_SLICE = 16,384 文字) に収まる ESC[2J の繰り返し
だけで、履歴を 1 行も増やさずに固まっていた。入力は '\x1b[2J' x 4096。

実測 (基準 81664d2、画面モデルだけ):
   24x80  0.214 秒 /  50x200 0.980 秒
  100x300 3.657 秒 / 200x500 15.008 秒
TerminalWidget (offscreen) で append_output + _flush_pending_output を
1 回流すと 24x80 0.303 秒、100x300 4.482 秒。

原因は 1 命令ごとに _record_screen が画面全体 (行 x 桁のセル) を走査し、
続けて画面の行を丸ごと作り直すこと。画面が既に空でも走査と作り直しが
丸ごと走っていた。7 周目で直した SU / DL の履歴の増殖とは別経路。

直し方: 画面が丸ごと空になったことを Screen._screen_blank で覚え、
次の消去では _record_screen の走査 (送る中身が無い) と行の作り直し
(同じ行になる) を省く。印が落ちるのは印字・set_size・代替画面の
出入り・RIS だけなので、覚えておけるのはそこまで。

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


class EraseDisplayRepeatCostTest(unittest.TestCase):
    def test_a_repeated_full_erase_does_not_rebuild_the_blank_screen(self):
        """2 回目以降の ED 2 が、空の画面の行を作り直さないこと。"""
        screen = Screen(100, 300)
        calls = []
        real = Screen._blank_line

        def counted(self):
            calls.append(1)
            return real(self)

        Screen._blank_line = counted
        try:
            feed(screen, (ESC + "[2J") * 64)
        finally:
            Screen._blank_line = real
        self.assertLessEqual(len(calls), screen.rows,
                             "空の画面を何度も作り直している")

    def test_a_repeated_full_erase_stays_fast_on_a_large_screen(self):
        """大きな画面でも、ED 2 の連打が待たされる時間にならないこと。"""
        screen = Screen(100, 300)
        events = Parser().feed((ESC + "[2J") * 4096)
        started = time.perf_counter()
        screen.apply(events)
        spent = time.perf_counter() - started
        self.assertLess(spent, 1.0, "ED 2 の連打が %.3f 秒かかる" % spent)


class EraseDisplayRepeatBehaviourTest(unittest.TestCase):
    def test_a_full_erase_still_sends_the_screen_to_the_history(self):
        """中身のある画面の ED 2 が、これまでどおり履歴へ送ること。"""
        screen = feed(Screen(4, 8), "one\r\ntwo\r\n")
        feed(screen, ESC + "[2J")
        self.assertEqual([row_text(line) for line in screen.history],
                         ["one", "two"])
        self.assertEqual([row_text(line)
                          for line, _ in screen.take_new_history()],
                         ["one", "two"])
        self.assertEqual(screen.text(), [""] * 4)

    def test_printing_after_an_erase_is_recorded_by_the_next_erase(self):
        """消去のあとに出た行が、次の消去で履歴へ落ちること。"""
        screen = feed(Screen(4, 8), ESC + "[2J")
        screen.take_new_history()
        feed(screen, ESC + "[H" + "later")
        feed(screen, ESC + "[2J")
        self.assertEqual([row_text(line) for line in screen.history],
                         ["later"])
        self.assertEqual([row_text(line)
                          for line, _ in screen.take_new_history()],
                         ["later"])

    def test_a_repeated_erase_does_not_record_empty_rows(self):
        """空の画面を繰り返し消しても、履歴が増えないこと。"""
        screen = feed(Screen(4, 8), (ESC + "[2J") * 10)
        self.assertEqual(len(screen.history), 0)
        self.assertEqual(screen.take_new_history(), [])

    def test_an_erase_after_a_resize_still_fits_the_new_width(self):
        """大きさを変えたあとの消去が、新しい桁の行を作ること。"""
        screen = feed(Screen(4, 8), ESC + "[2J")
        screen.set_size(4, 4)
        feed(screen, ESC + "[2J")
        self.assertEqual([len(line) for line in screen.lines], [4] * 4)

    def test_an_erase_on_the_alternate_screen_keeps_the_main_one(self):
        """代替画面を消しても、戻ったメイン画面の中身が残ること。"""
        screen = feed(Screen(4, 8), "kept")
        feed(screen, ESC + "[?1049h" + ESC + "[2J" + ESC + "[2J")
        feed(screen, ESC + "[?1049l")
        self.assertEqual(screen.text()[0], "kept")
        feed(screen, ESC + "[2J")
        self.assertEqual([row_text(line) for line in screen.history], ["kept"])

    def test_a_wrap_mark_made_after_an_erase_is_dropped_by_the_next(self):
        """消去のあとに付いた折り返しの印が、次の消去で外れること。"""
        screen = feed(Screen(4, 4), ESC + "[2J")
        feed(screen, ESC + "[H" + "ABCDE")
        self.assertTrue(screen.wrapped[0], "前提: 折り返しの印が立たない")
        feed(screen, ESC + "[2J")
        self.assertEqual(screen.wrapped, [False] * 4)


if __name__ == "__main__":
    unittest.main()
