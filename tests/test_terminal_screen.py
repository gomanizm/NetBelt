"""画面モデルの検証。Qt を使わない。

前半は遷移図ならぬ ECMA-48 の意味の検証 (印字・折り返し・消去・移動)。
後半は実機採取データで、旧実装が検証済みの見え方をこのモデルでも
再現できることを固定する。数値はすべて実測。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402
from core.terminal.attrs import DEFAULT     # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures")


def fixture(name):
    raw = io.open(os.path.join(FIXTURES, name), "rb").read()
    return raw.decode("utf-8", "replace")


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def everything(screen):
    """履歴 + 画面。セッションとして人の目に入る全行。"""
    scrolled = ["".join(cell[0] for cell in line).rstrip()
                for line in screen.history]
    return scrolled + screen.text()


class PrintingTest(unittest.TestCase):
    def test_text_lands_where_the_cursor_is(self):
        s = feed(Screen(), "show version")
        self.assertEqual(s.text()[0], "show version")
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 12))

    def test_backspace_overwrites_like_a_device_pager(self):
        s = feed(Screen(), "abc\b\bX")
        self.assertEqual(s.text()[0], "aXc")

    def test_cr_returns_lf_descends(self):
        s = feed(Screen(), "one\r\ntwo")
        self.assertEqual(s.text()[:2], ["one", "two"])

    def test_bare_lf_keeps_the_column(self):
        s = feed(Screen(), "abc\ndef")
        self.assertEqual(s.text()[:2], ["abc", "   def"])

    def test_tab_jumps_to_the_next_stop(self):
        s = feed(Screen(), "a\tb")
        self.assertEqual(s.text()[0], "a       b")

    def test_the_81st_character_wraps(self):
        s = feed(Screen(), "x" * 81)
        self.assertEqual(s.text()[0], "x" * 80)
        self.assertEqual(s.text()[1], "x")

    def test_a_cr_at_the_right_edge_does_not_make_a_phantom_line(self):
        # 80 桁ちょうどの行は、右端で折り返しを保留する (VT100 の癖)。
        # 続く CR LF で空行が湧いてはいけない
        s = feed(Screen(), "x" * 80 + "\r\nnext")
        self.assertEqual(s.text()[:3], ["x" * 80, "next", ""])

    def test_del_is_invisible(self):
        s = feed(Screen(), "a\x7fb")
        self.assertEqual(s.text()[0], "ab")


class ScrollAndHistoryTest(unittest.TestCase):
    def test_lines_pushed_off_the_top_become_history(self):
        s = feed(Screen(), "\r\n".join("line%d" % i for i in range(30)))
        self.assertEqual(len(s.history), 6)     # 30 行 - 画面 24 行
        self.assertEqual("".join(c[0] for c in s.history[0]).rstrip(),
                         "line0")
        self.assertEqual(s.text()[0], "line6")

    def test_new_history_is_handed_over_once(self):
        s = feed(Screen(), "\r\n".join("l%d" % i for i in range(26)))
        self.assertEqual(len(s.take_new_history()), 2)
        self.assertEqual(s.take_new_history(), [])

class CursorMovementTest(unittest.TestCase):
    def test_cup_is_one_based(self):
        s = feed(Screen(), "\x1b[3;5HX")
        self.assertEqual(s.text()[2], "    X")

    def test_cup_without_params_goes_home(self):
        s = feed(Screen(), "hello\x1b[HX")
        self.assertEqual(s.text()[0], "Xello")

    def test_cup_clamps_to_the_screen(self):
        s = feed(Screen(), "\x1b[99;999HX")
        self.assertEqual((s.cursor_row, s.cursor_col), (23, 79))

    def test_relative_moves(self):
        s = feed(Screen(), "\x1b[5;5H\x1b[2A\x1b[3C\x1b[1B\x1b[6DX")
        # (4,4) -> 上2 (2,4) -> 右3 (2,7) -> 下1 (3,7) -> 左6 (3,1)
        self.assertEqual(s.text()[3], " X")

    def test_column_address(self):
        s = feed(Screen(), "abcdef\x1b[3G_")
        self.assertEqual(s.text()[0], "ab_def")


class AttributeTest(unittest.TestCase):
    def test_sgr_travels_with_the_characters(self):
        s = feed(Screen(), "a\x1b[7mb\x1b[mc")
        row = s.lines[0]
        self.assertEqual(row[0][1], DEFAULT)
        self.assertTrue(row[1][1].reverse)
        self.assertEqual(row[2][1], DEFAULT)


class UnknownSequenceTest(unittest.TestCase):
    def test_an_unknown_final_changes_nothing(self):
        s = feed(Screen(), "abc\x1b[999Xdef")
        self.assertEqual(s.text()[0], "abcdef")


class CiscoOnTheScreenModelTest(unittest.TestCase):
    """実機採取 7058 バイト。旧実装で検証済みの見え方を固定する。"""

    @classmethod
    def setUpClass(cls):
        cls.screen = feed(Screen(), fixture("cisco_ios_vt100.bin"))
        cls.full = everything(cls.screen)

    def test_the_pager_prompt_is_erased(self):
        leftover = [l for l in self.full if "More" in l]
        self.assertEqual(leftover, [])

    def test_a_typo_corrected_with_backspace_renders_clean(self):
        self.assertTrue(any("show version" in l for l in self.full))
        self.assertFalse(any("verzz" in l for l in self.full))

    def test_the_output_keeps_its_columns(self):
        marker = [i for i, l in enumerate(self.full) if l.strip() == "^"]
        self.assertEqual(len(marker), 1)
        at = marker[0]
        self.assertEqual(self.full[at].index("^"), 10)
        self.assertIn("Invalid input", self.full[at + 1])
        indented = [l for l in self.full
                    if l.startswith("       ") and l.strip()]
        self.assertEqual(len(indented), 12)

    def test_every_split_point_shows_the_same_session(self):
        raw = fixture("cisco_ios_vt100.bin")
        for cut in range(1, len(raw), 7):
            p = Parser()
            s = Screen()
            s.apply(p.feed(raw[:cut]))
            s.apply(p.feed(raw[cut:]))
            if everything(s) != self.full:
                self.fail("%d バイト目で切ると画面が変わる" % cut)


if __name__ == "__main__":
    unittest.main()
