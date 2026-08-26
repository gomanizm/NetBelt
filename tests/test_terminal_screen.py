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


class ErasingTest(unittest.TestCase):
    def test_erase_to_end_of_line(self):
        s = feed(Screen(), "abcdef\x1b[4G\x1b[K")
        self.assertEqual(s.text()[0], "abc")

    def test_erase_to_start_of_line(self):
        s = feed(Screen(), "abcdef\x1b[3G\x1b[1K")
        self.assertEqual(s.text()[0], "   def")

    def test_erase_whole_line_leaves_the_cursor(self):
        s = feed(Screen(), "abcdef\x1b[2KX")
        self.assertEqual(s.text()[0], "      X")

    def test_erase_below(self):
        s = feed(Screen(), "aaa\r\nbbb\r\nccc\x1b[2;2H\x1b[J")
        self.assertEqual(s.text()[:3], ["aaa", "b", ""])

    def test_erase_a_run_of_characters(self):
        s = feed(Screen(), "abcdef\x1b[2G\x1b[3X")
        self.assertEqual(s.text()[0], "a   ef")

    def test_erasing_the_display_loses_no_session_record(self):
        # clear で記録を失わない (v1.1.1 の方針)。消される直前に
        # 見えていた行は履歴へ移る
        s = feed(Screen(), "\r\n".join("l%d" % i for i in range(30)))
        feed(s, "\x1b[2J")
        self.assertEqual(s.text(), [""] * 24)
        record = everything(s)
        for i in range(30):
            self.assertIn("l%d" % i, record)

    def test_erased_cells_are_undressed(self):
        s = feed(Screen(), "\x1b[7mabc\x1b[2K")
        self.assertEqual(s.lines[0][1], (" ", DEFAULT))


class EditingTest(unittest.TestCase):
    def test_inserted_lines_push_the_rest_down(self):
        s = feed(Screen(), "aaa\r\nbbb\r\nccc\x1b[2;1H\x1b[L")
        self.assertEqual(s.text()[:4], ["aaa", "", "bbb", "ccc"])

    def test_deleted_lines_pull_the_rest_up(self):
        s = feed(Screen(), "aaa\r\nbbb\r\nccc\x1b[2;1H\x1b[M")
        self.assertEqual(s.text()[:3], ["aaa", "ccc", ""])

    def test_inserted_chars_push_the_line_right(self):
        s = feed(Screen(), "abcdef\x1b[3G\x1b[2@")
        self.assertEqual(s.text()[0], "ab  cdef")

    def test_deleted_chars_pull_the_line_left(self):
        s = feed(Screen(), "abcdef\x1b[3G\x1b[2P")
        self.assertEqual(s.text()[0], "abef")

    def test_the_line_stays_exactly_as_wide(self):
        s = feed(Screen(), "abc\x1b[1;1H\x1b[3@")
        self.assertEqual(len(s.lines[0]), 80)
        feed(s, "\x1b[3P")
        self.assertEqual(len(s.lines[0]), 80)


class ScrollRegionTest(unittest.TestCase):
    def test_decstbm_confines_the_scroll(self):
        s = Screen()
        feed(s, "top\x1b[2;3r")         # 2..3 行目だけがスクロールする
        feed(s, "\x1b[3;1Haaa\r\nbbb\r\nccc")
        self.assertEqual(s.text()[:4], ["top", "bbb", "ccc", ""])

    def test_a_partial_region_never_feeds_history(self):
        s = Screen()
        feed(s, "\x1b[2;3r\x1b[3;1H" + "\r\n".join("x%d" % i
                                                   for i in range(10)))
        self.assertEqual(len(s.history), 0)

    def test_decstbm_homes_the_cursor(self):
        s = feed(Screen(), "\x1b[5;5H\x1b[2;10rX")
        self.assertEqual(s.text()[0], "X")

    def test_nonsense_margins_are_refused(self):
        s = feed(Screen(), "\x1b[7;3r")
        self.assertEqual((s.scroll_top, s.scroll_bottom), (0, 23))


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


class EscDispatchTest(unittest.TestCase):
    def test_save_and_restore_cursor_with_attributes(self):
        s = feed(Screen(), "\x1b[3;3H\x1b[7m\x1b7\x1b[H\x1b[m\x1b8X")
        self.assertEqual(s.text()[2], "  X")
        self.assertTrue(s.lines[2][2][1].reverse)

    def test_reverse_index_at_the_top_scrolls_down(self):
        s = feed(Screen(), "top\x1b[H\x1bMnew")
        self.assertEqual(s.text()[:2], ["new", "top"])

    def test_full_reset_clears_the_screen_but_not_the_history(self):
        s = feed(Screen(), "\r\n".join("l%d" % i for i in range(30)))
        feed(s, "\x1bc")
        self.assertEqual(s.text(), [""] * 24)
        self.assertEqual(len(s.history), 6)

    def test_line_drawing_characters(self):
        # ESC)0 で G1 に罫線集合を指示し、SO で使い、SI で戻る
        s = feed(Screen(), "\x1b)0\x0elqk\x0fx")
        self.assertEqual(s.text()[0], "┌─┐x")


class ModeTest(unittest.TestCase):
    def test_autowrap_off_pins_the_cursor_to_the_edge(self):
        s = feed(Screen(), "\x1b[?7l\x1b[76Gabcdef")
        self.assertTrue(s.text()[0].endswith("abcdf"))
        self.assertEqual(len(s.text()[0]), 80)

    def test_input_side_flags_are_exposed(self):
        s = Screen()
        self.assertFalse(s.application_cursor_keys)
        self.assertFalse(s.bracketed_paste)
        feed(s, "\x1b[?1h\x1b[?2004h")
        self.assertTrue(s.application_cursor_keys)
        self.assertTrue(s.bracketed_paste)
        feed(s, "\x1b[?1l\x1b[?2004l")
        self.assertFalse(s.application_cursor_keys)
        self.assertFalse(s.bracketed_paste)

    def test_cursor_visibility_is_tracked(self):
        s = feed(Screen(), "\x1b[?25l")
        self.assertFalse(s.cursor_visible)


class AlternateScreenTest(unittest.TestCase):
    def test_leaving_the_alt_screen_restores_the_shell(self):
        s = feed(Screen(), "user@lab:~$ vi")
        feed(s, "\x1b[?1049h~~vi screen~~\x1b[?1049l")
        self.assertEqual(s.text()[0], "user@lab:~$ vi")
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 14))

    def test_the_alt_screen_starts_blank(self):
        s = feed(Screen(), "shell stuff\x1b[?1049h")
        self.assertEqual(s.text(), [""] * 24)

    def test_alt_screen_scrolling_never_reaches_history(self):
        s = feed(Screen(), "\x1b[?1049h")
        feed(s, "\r\n".join("frame%d" % i for i in range(40)))
        self.assertEqual(len(s.history), 0)


class ResponseTest(unittest.TestCase):
    def test_cursor_position_report(self):
        s = feed(Screen(), "abc\x1b[6n")
        self.assertEqual(s.take_responses(), ["\x1b[1;4R"])
        self.assertEqual(s.take_responses(), [])

    def test_device_attributes(self):
        s = feed(Screen(), "\x1b[c")
        self.assertEqual(s.take_responses(), ["\x1b[?1;2c"])


class TitleTest(unittest.TestCase):
    def test_osc_sets_the_title(self):
        s = feed(Screen(), "\x1b]0;user@lab: ~\x07")
        self.assertEqual(s.title, "user@lab: ~")


class ResizeTest(unittest.TestCase):
    def test_wider_lines_keep_their_content(self):
        s = feed(Screen(), "show version")
        s.set_size(24, 132)
        self.assertEqual(s.text()[0], "show version")
        self.assertEqual(len(s.lines[0]), 132)

    def test_narrower_lines_are_cut_not_rewrapped(self):
        s = feed(Screen(), "0123456789")
        s.set_size(24, 8)
        self.assertEqual(s.text()[0], "01234567")

    def test_shrinking_rows_prefers_dropping_blank_bottom_lines(self):
        s = feed(Screen(), "keep me")
        s.set_size(10, 80)
        self.assertEqual(s.text()[0], "keep me")
        self.assertEqual(len(s.history), 0)

    def test_shrinking_rows_saves_written_lines_to_history(self):
        s = feed(Screen(), "\r\n".join("l%d" % i for i in range(24)))
        s.set_size(10, 80)
        self.assertEqual(len(s.history), 14)
        self.assertEqual(s.text()[0], "l14")
        self.assertEqual((s.cursor_row, s.cursor_col), (9, 3))

    def test_the_scroll_region_snaps_back_to_full(self):
        s = feed(Screen(), "\x1b[5;10r")
        s.set_size(30, 80)
        self.assertEqual((s.scroll_top, s.scroll_bottom), (0, 29))


class NanoOnTheScreenModelTest(unittest.TestCase):
    """nano の実データ (527 バイト)。旧実装では 1 行に潰れていた画面が
    24×80 に正しく組み上がることを固定する。値は実測。"""

    @classmethod
    def setUpClass(cls):
        cls.screen = feed(Screen(), fixture("nano_vt100.bin"))

    def test_the_title_bar_sits_on_row_0(self):
        self.assertIn("GNU nano", self.screen.text()[0])
        self.assertIn("New Buffer", self.screen.text()[0])

    def test_the_shortcut_bars_sit_at_the_bottom(self):
        self.assertTrue(self.screen.text()[22].startswith("^G Help"))
        self.assertTrue(self.screen.text()[23].startswith("^X Exit"))

    def test_the_cursor_waits_in_the_editing_area(self):
        self.assertEqual((self.screen.cursor_row, self.screen.cursor_col),
                         (1, 0))

    def test_the_title_bar_is_reverse_video(self):
        # "GNU nano 7.2" の非空白 10 文字 + "New Buffer" の 9 文字
        reversed_cells = [cell for cell in self.screen.lines[0]
                          if cell[1].reverse and cell[0] != " "]
        self.assertEqual(len(reversed_cells), 19)


class ClearOnTheScreenModelTest(unittest.TestCase):
    """clear の実データ (255 バイト)。ESC[H ESC[J と NUL パディング。"""

    def test_the_screen_empties_but_the_record_survives(self):
        s = feed(Screen(), fixture("clear_vt100.bin"))
        self.assertEqual([l for l in s.text() if l], ["user@lab:~$"])
        self.assertIn("clear", everything(s))


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
