"""画面モデルの検証。Qt を使わない。

前半は遷移図ならぬ ECMA-48 の意味の検証 (印字・折り返し・消去・移動)。
後半は実機採取データで、旧実装が検証済みの見え方をこのモデルでも
再現できることを固定する。数値はすべて実測。
"""
import io
import os
import sys
import time
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

    def test_erase_above_from_the_bottom_right_keeps_the_record(self):
        # ESC[1J を最下行の右端から送ると画面は丸ごと空白になる。
        # 消える中身は ED 2 と同じなので、履歴にも同じだけ残る
        s = feed(Screen(rows=3, cols=10), "one\r\ntwo\r\nthree")
        feed(s, "\x1b[3;10H\x1b[1J")
        self.assertEqual(s.text(), ["", "", ""])
        record = everything(s)
        for want in ("one", "two", "three"):
            self.assertIn(want, record)

    def test_erase_above_with_nothing_below_keeps_the_record(self):
        # 右端でなくても、カーソルより下に中身が無ければ ED 1 で
        # 画面は丸ごと空白になる。消える中身は ED 2 と同じ
        s = feed(Screen(rows=3, cols=10), "one\r\ntwo\r\nthree")
        feed(s, "\x1b[3;6H\x1b[1J")
        self.assertEqual(s.text(), ["", "", ""])
        record = everything(s)
        for want in ("one", "two", "three"):
            self.assertIn(want, record)

    def test_erase_above_on_a_fresh_screen_keeps_the_record(self):
        # 24x80 の既定の画面で 2 行出した直後の ESC[1J。最下行でも
        # 右端でもないが、消えれば画面には何も残らない
        s = feed(Screen(), "banner\r\nsecond")
        feed(s, "\x1b[1J")
        self.assertEqual([t for t in s.text() if t], [])
        record = everything(s)
        for want in ("banner", "second"):
            self.assertIn(want, record)

    def test_erase_above_leaving_text_below_keeps_the_screen(self):
        # 下に中身が残るなら画面は消えていない。履歴へ送ると同じ行が
        # 画面と履歴の両方に二重に残る
        s = feed(Screen(rows=3, cols=10), "one\r\ntwo\r\nthree")
        feed(s, "\x1b[1;2H\x1b[1J")
        self.assertEqual(s.text(), ["  e", "two", "three"])
        self.assertEqual(list(s.history), [])

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

    def test_il_returns_the_cursor_to_the_left_margin(self):
        # DEC の IL/DL はカーソルを左マージンへ戻す (xterm も同じ)。
        # 戻さないと、直後に位置指定なしで印字したとき桁がずれる
        s = feed(Screen(), "\x1b[3;5H\x1b[LX")
        self.assertEqual(s.text()[2], "X")
        self.assertEqual((s.cursor_row, s.cursor_col), (2, 1))

    def test_dl_returns_the_cursor_to_the_left_margin(self):
        s = feed(Screen(), "\x1b[3;5H\x1b[MX")
        self.assertEqual(s.text()[2], "X")
        self.assertEqual((s.cursor_row, s.cursor_col), (2, 1))

    def test_a_refused_il_leaves_the_cursor_alone(self):
        # 範囲の外では IL/DL 自体が効かないので、桁も動かさない
        s = feed(Screen(), "\x1b[5;10r\x1b[2;5H\x1b[L")
        self.assertEqual((s.cursor_row, s.cursor_col), (1, 4))

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

    def test_ich_on_a_widened_wrapped_row_keeps_the_last_character(self):
        """折り返し行は広げても埋めないので、桁数より短いことがある。

        そこへ ICH すると、桁に余裕があるのに行末の文字が捨てられていた
        (4 桁で "ABCDE" → 8 桁へ広げて CUP 1;3 ESC[@ で 'D' が消えた)。
        押し出すのは行が桁数いっぱいのときだけ。
        """
        s = feed(Screen(rows=3, cols=4), "ABCDE")
        s.set_size(3, 8)
        feed(s, "\x1b[1;3H\x1b[@")
        self.assertEqual(s.text()[0], "AB CD")
        self.assertLessEqual(len(s.lines[0]), 8, "行が桁数を超えて伸びた")

    def test_ich_on_a_full_row_still_pushes_the_last_character_off(self):
        s = feed(Screen(rows=3, cols=8), "ABCDEFGH\x1b[1;3H\x1b[@")
        self.assertEqual(s.text()[0], "AB CDEFG")
        self.assertEqual(len(s.lines[0]), 8)


class WrapMarkTest(unittest.TestCase):
    """折り返しの印は、折り返しで付き、その行への印字・消去で外れる。

    印が残ったまま行が書き直されると、履歴へ押し出された時点で次の
    行と改行なしに連結される (コピー・ログ保存の行境界が変わる)。
    EL を伴う書き直しでは外れていたが、CUP + 印字だけの全画面型
    再描画や ECH では残っていた。
    """

    def _wrapped_then_rewritten(self, rewrite):
        s = feed(Screen(rows=4, cols=4), "ABCDE")
        self.assertTrue(s.wrapped[0], "前提: 折り返しの印が付いていない")
        return feed(s, rewrite)

    def test_printing_on_the_row_clears_the_mark(self):
        s = self._wrapped_then_rewritten("\x1b[1;1HPING\x1b[2;1HPONG")
        self.assertEqual(s.text()[:2], ["PING", "PONG"])
        self.assertFalse(s.wrapped[0], "書き直した行に折り返しの印が残っている")

    def test_the_rewritten_rows_reach_the_history_as_two_lines(self):
        s = self._wrapped_then_rewritten("\x1b[1;1HPING\x1b[2;1HPONG")
        feed(s, "\x1b[4;1H\r\n\r\n")          # 2 行押し出す
        self.assertEqual([("".join(c[0] for c in line).rstrip(), w)
                          for line, w in s.take_new_history()],
                         [("PING", False), ("PONG", False)])

    def test_erase_characters_clears_the_mark(self):
        s = self._wrapped_then_rewritten("\x1b[1;1H\x1b[4X")
        self.assertFalse(s.wrapped[0], "ECH した行に折り返しの印が残っている")

    def test_a_rewrite_that_wraps_again_keeps_the_mark(self):
        s = self._wrapped_then_rewritten("\x1b[1;1HWXYZQ")
        self.assertEqual(s.text()[:2], ["WXYZ", "Q"])
        self.assertTrue(s.wrapped[0], "改めて折り返したのに印が無い")

    def test_a_rewrite_up_to_the_right_edge_keeps_the_mark(self):
        """右端ちょうどまで書き直しても、次の行へ続くまま。

        readline や vim が折り返した行を CUP + 印字だけで描き直すときの
        形。印を外すと、次の行 (続き) との間に無いはずの改行が入る。
        """
        s = self._wrapped_then_rewritten("\x1b[1;1HWXYZ")
        self.assertEqual(s.text()[:2], ["WXYZ", "E"])
        self.assertTrue(s.wrapped[0],
                        "右端まで書き直した行の折り返しの印が外れた")

    def test_the_rewritten_wrapped_row_reaches_the_history_joined(self):
        s = self._wrapped_then_rewritten("\x1b[1;1HWXYZ")
        feed(s, "\x1b[4;1H\r\n\r\n")          # 2 行押し出す
        self.assertEqual([("".join(c[0] for c in line).rstrip(), w)
                          for line, w in s.take_new_history()],
                         [("WXYZ", True), ("E", False)])

    def test_el1_that_reaches_the_right_edge_clears_the_mark(self):
        """EL 1 が行末まで届いたら、その行から次への続きは無い。

        EL 1 は「行頭からカーソルまで」なので普段は行末に届かず、印を
        外さないのが正しい。届いたときだけは行が丸ごと空になるので、
        印を残すと履歴・コピーで空行の境界が消えて字下げが増える。
        """
        s = self._wrapped_then_rewritten("\x1b[1;4H\x1b[1K")
        self.assertEqual(s.text()[0], "")
        self.assertFalse(s.wrapped[0], "空になった行に折り返しの印が残っている")

    def test_el1_that_stops_short_keeps_the_mark(self):
        s = self._wrapped_then_rewritten("\x1b[1;3H\x1b[1K")
        self.assertTrue(s.wrapped[0], "行末へ届いていないのに印が外れた")

    def test_the_emptied_row_reaches_the_history_as_its_own_line(self):
        s = self._wrapped_then_rewritten("\x1b[1;4H\x1b[1K")
        feed(s, "\x1b[4;1H\r\n\r\n")          # 2 行押し出す
        self.assertEqual([("".join(c[0] for c in line).rstrip(), w)
                          for line, w in s.take_new_history()],
                         [("", False), ("E", False)])

    def test_filling_a_row_to_the_edge_does_not_create_a_mark(self):
        """折り返していない行に、印字だけで印が付いてはいけない。"""
        s = feed(Screen(rows=4, cols=4), "ABCD\x1b[2;1HPONG")
        self.assertEqual([s.wrapped[0], s.wrapped[1]], [False, False])


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


class RunawayParameterTest(unittest.TestCase):
    """暴走したパラメータで画面処理が止まらなくならないこと。

    パラメータは 256 桁まで書けるので、頭打ちにしないと 10^250 回の
    ループになる。apply は GUI スレッドから呼ばれるので、そうなると
    強制終了以外に戻る手立てが無い。
    """
    HUGE = "9" * 200

    def test_every_repeating_command_finishes_at_once(self):
        for final in "STLM@PX":
            with self.subTest(command=final):
                s = Screen()
                start = time.time()
                feed(s, "\x1b[" + self.HUGE + final)
                self.assertLess(time.time() - start, 1.0,
                                "ESC[<巨大数>%s が終わらない" % final)

    def test_a_huge_scroll_does_not_flood_the_history(self):
        s = Screen()
        feed(s, "\x1b[" + self.HUGE + "S")
        self.assertLessEqual(len(s.take_new_history()), s.rows)

    def test_the_result_still_matches_a_full_screen_operation(self):
        # 頭打ちにしても、意味は「画面全部」のままであること
        huge = feed(Screen(), "a\r\nb\r\nc\x1b[" + self.HUGE + "S")
        full = feed(Screen(), "a\r\nb\r\nc\x1b[24S")
        self.assertEqual(huge.text(), full.text())


class ScrollbackEraseTest(unittest.TestCase):
    def test_ed3_leaves_the_visible_screen_alone(self):
        """ESC[3J は履歴を消す命令で、見えている画面には触らない。

        NetBelt は記録を消さない方針なので何もしない。画面まで消すと
        clear -x を打っただけで表示が飛ぶ。
        """
        s = feed(Screen(), "visible text")
        feed(s, "\x1b[3J")
        self.assertEqual(s.text()[0], "visible text")
        self.assertEqual(len(s.history), 0)


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
    def test_an_undefined_erase_parameter_changes_nothing(self):
        # ED に定義があるのは 0-3、EL は 0-2 だけ (XTerm ctlseqs)。
        # それ以外の値を全消去として扱うと、機器が出した
        # 行が黙って画面から消える
        for seq in ("\x1b[4J", "\x1b[9J", "\x1b[3K", "\x1b[9K"):
            with self.subTest(seq=seq):
                s = feed(Screen(), "KEEP" + seq)
                self.assertEqual(s.text()[0], "KEEP")


class IntermediateByteTest(unittest.TestCase):
    """中間バイト付きの列は別の命令。最終文字だけで既知命令と取り違えない。

    ESC % c が RIS、ESC[?1049$h が代替画面切替として実行されていた。
    実在する列でも ESC # 8 (DECALN) が DECRC、ESC * E (G2 指示) が
    NEL に化けてカーソルがずれる。
    """

    def test_esc_with_an_intermediate_is_not_ris(self):
        s = feed(Screen(), "KEEP\x1b%c")
        self.assertEqual(s.text()[0], "KEEP")
        self.assertEqual(len(s.history), 0)

    def test_csi_private_mode_with_an_intermediate_is_ignored(self):
        s = feed(Screen(), "MAIN\x1b[?1049$hALT")
        self.assertFalse(s.alt_active)
        self.assertEqual(s.text()[0], "MAINALT")

    def test_decaln_is_not_mistaken_for_decrc(self):
        s = feed(Screen(), "AB\x1b[2;3H\x1b#8Z")
        self.assertEqual(s.text()[:2], ["AB", "  Z"])

    def test_a_g2_designation_is_not_mistaken_for_nel(self):
        s = feed(Screen(), "AB\x1b*EZ")
        self.assertEqual(s.text()[:2], ["ABZ", ""])

    def test_charset_designation_still_works(self):
        s = feed(Screen(), "\x1b(0lqk\x1b(Bx")
        self.assertEqual(s.text()[0], "┌─┐x")


class EscDispatchTest(unittest.TestCase):
    def test_save_and_restore_cursor_with_attributes(self):
        s = feed(Screen(), "\x1b[3;3H\x1b[7m\x1b7\x1b[H\x1b[m\x1b8X")
        self.assertEqual(s.text()[2], "  X")
        self.assertTrue(s.lines[2][2][1].reverse)

    def test_save_and_restore_cursor_keeps_the_charset(self):
        # DECSC は位置と属性だけでなく、文字集合の指示も保存
        # する (VT100/xterm)。復元した後の罫線が ASCII のまま出ていた
        s = feed(Screen(), "\x1b(0\x1b7\x1b(B\x1b8lqk")
        self.assertEqual(s.text()[0], "┌─┐")

    def test_save_and_restore_cursor_keeps_the_shift_state(self):
        # SO で G1 を使っている状態も DECSC/DECRC で行き来する
        s = feed(Screen(), "\x1b)0\x0e\x1b7\x0f\x1b8lqk")
        self.assertEqual(s.text()[0], "┌─┐")

    def test_save_and_restore_cursor_keeps_the_pending_wrap(self):
        # 右端ちょうどで DECSC/DECRC を挟んでも折り返し待ちは残る
        # (xterm は wrap_flag を DECSC の保存対象に含める)。落とすと
        # 次の 1 文字が折り返さず右端の文字を上書きしていた
        s = feed(Screen(), "\x1b[1;80Ha\x1b7\x1b8b")
        self.assertEqual(s.text()[0][-1], "a")
        self.assertEqual(s.text()[1], "b")

    def test_reverse_index_at_the_top_scrolls_down(self):
        s = feed(Screen(), "top\x1b[H\x1bMnew")
        self.assertEqual(s.text()[:2], ["new", "top"])

    def test_full_reset_clears_the_screen_but_not_the_history(self):
        # RIS (reset / tput reset / 一部機器の起動コンソール) も ED 2 と
        # 同じく、消す直前に見えていた行を履歴へ送る。旧契約は「押し出し
        # 済みの 6 行だけ残る」で、画面上の 24 行が記録から消えていた
        s = feed(Screen(), "\r\n".join("l%d" % i for i in range(30)))
        feed(s, "\x1bc")
        self.assertEqual(s.text(), [""] * 24)
        self.assertEqual(len(s.history), 30)
        record = everything(s)
        for i in range(30):
            self.assertIn("l%d" % i, record)

    def test_full_reset_hands_the_wiped_lines_to_the_renderer(self):
        s = feed(Screen(), "KEEP\x1bc")
        self.assertEqual([("".join(c[0] for c in line).rstrip(), w)
                          for line, w in s.take_new_history()],
                         [("KEEP", False)])

    def test_full_reset_on_the_alt_screen_saves_the_shell_behind_it(self):
        # vi の中で reset が飛んでも、裏に退避していたシェル画面は
        # 記録に残る。代替画面の中身は (これまでどおり) 記録しない
        s = feed(Screen(), "MAIN-A\r\nMAIN-B")
        feed(s, "\x1b[?1049hALT-ONLY\x1bc")
        self.assertFalse(s.alt_active)
        self.assertEqual(s.text(), [""] * 24)
        record = everything(s)
        self.assertIn("MAIN-A", record)
        self.assertIn("MAIN-B", record)
        self.assertNotIn("ALT-ONLY", record)

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

    def test_the_alt_screen_round_trip_keeps_the_pending_wrap(self):
        # 1049 の保存・復元も DECSC 相当なので折り返し待ちを持ち帰る
        s = feed(Screen(), "\x1b[1;80Ha")
        feed(s, "\x1b[?1049h\x1b[?1049lb")
        self.assertEqual(s.text()[0][-1], "a")
        self.assertEqual(s.text()[1], "b")

    def test_entering_the_alt_screen_keeps_the_cursor(self):
        # xterm の 1049 入場は CursorSave → ToAlternate → ClearScreen で、
        # ClearScreen はカーソルを動かさない。ここだけホームへ戻すと
        # 1047/47 とも食い違っていた
        s = feed(Screen(), "shell\r\n\x1b[3;5H\x1b[?1049hX")
        self.assertEqual((s.cursor_row, s.cursor_col), (2, 5))
        self.assertEqual(s.text()[2], "    X")
        self.assertEqual(s.text()[0], "")

    def test_the_alt_screen_has_its_own_decsc_slot(self):
        # xterm は DECSC の保存領域を画面ごとに持つ (screen->sc[])。
        # 共有すると、代替画面のアプリが撃った ESC 7 がメイン画面の
        # 保存位置を潰し、戻ってきた ESC 8 が別の行へ飛んでいた
        s = feed(Screen(), "\x1b[5;10H\x1b7")
        feed(s, "\x1b[?1049h\x1b[20;70H\x1b7\x1b[?1049l")
        feed(s, "\x1b8X")
        self.assertEqual((s.cursor_row, s.cursor_col), (4, 10))
        self.assertEqual(s.text()[4], " " * 9 + "X")

    def test_the_alt_screen_starts_blank(self):
        s = feed(Screen(), "shell stuff\x1b[?1049h")
        self.assertEqual(s.text(), [""] * 24)

    def test_re_entering_the_alt_screen_with_47_keeps_its_content(self):
        # 入場で白紙にするのは 1049 だけ (XTerm ctlseqs)。
        # 47 は裏画面の中身をそのまま見せる
        s = feed(Screen(), "shell\x1b[?47h\x1b[HALT\x1b[?47l")
        self.assertEqual(s.text()[0], "shell")
        feed(s, "\x1b[?47h")
        self.assertEqual(s.text()[0], "ALT")

    def test_leaving_the_alt_screen_with_1047_clears_it(self):
        # 1047 は退場のときに代替画面を消すので、次の入場は白紙
        s = feed(Screen(), "shell\x1b[?1047h\x1b[HALT\x1b[?1047l")
        self.assertEqual(s.text()[0], "shell")
        feed(s, "\x1b[?1047h")
        self.assertEqual(s.text()[0], "")

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

    def test_narrowing_leaves_a_written_line_alone(self):
        """桁が狭くなっても、書かれた行には触らないこと。

        切り捨てると窓を縮めただけで末尾が永久に失われ、割り直すと
        窓を往復するたびに切れ目が変わって中身が削れていった
        (どちらも実機試験で出た)。実端末も書かれた行は組み直さない。
        狭いときの見た目は表示側が折り返して面倒を見る。
        """
        s = feed(Screen(), "0123456789")
        s.set_size(24, 8)
        self.assertEqual(s.text()[0], "0123456789")

    def test_a_round_trip_changes_nothing(self):
        """縮めて戻す、を繰り返しても中身が一文字も変わらないこと。

        実機試験で「最小→戻すを繰り返すたびに 1 行ずつ消えていく」と
        報告された。組み直しをやめたので、往復は何もしないのと同じ。
        """
        key = "ssh-ed25519 " + "A" * 68 + " user@example.com"
        s = Screen(24, 120)
        feed(s, "$ cat ~/.ssh/authorized_keys\r\n" + key + "\r\n$ ")
        before = s.text()
        for _ in range(5):
            s.set_size(5, 20)
            s.set_size(24, 120)
        self.assertEqual(s.text(), before)

    def test_repeated_narrowing_still_keeps_everything(self):
        key = "ssh-ed25519 " + "A" * 68 + " user@example.com"
        s = Screen(24, 120)
        feed(s, key)
        for cols in (100, 80, 60, 40, 30):
            s.set_size(24, cols)
        self.assertEqual("".join(s.text()).replace(" ", ""),
                         key.replace(" ", ""))

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

    def test_a_long_line_stays_one_line_through_a_round_trip(self):
        """長い行は、縮めても刻まれず、戻しても元のままであること。

        実機試験で ls /etc/ の出力が刻まれたまま戻らなくなった。
        刻まないので、戻す必要も無い。
        """
        line = "x" * 100
        s = Screen(24, 120)
        feed(s, line)
        s.set_size(24, 40)
        self.assertEqual(s.text()[0], line)
        self.assertEqual(s.text()[1], "")
        s.set_size(24, 120)
        self.assertEqual(s.text()[0], line)

    def test_widening_does_not_pad_a_wrapped_line(self):
        """折り返し行を、広げたときに空白で埋めないこと。

        行の長さがそのまま「どこで折り返したか」なので、埋めると
        繋いだときに埋め草ぶんの隙間が開く。実機試験で、窓を広げると
        鍵の途中に空白が入ると報告された。
        """
        line = "x" * 100
        s = Screen(24, 73)
        feed(s, line)
        self.assertTrue(s.wrapped[0])
        s.set_size(24, 120)
        self.assertEqual(len(s.lines[0]), 73, "折り返し行が埋められた")
        joined = ("".join(c[0] for c in s.lines[0])
                  + "".join(c[0] for c in s.lines[1]).rstrip())
        self.assertEqual(joined, line)

    def test_a_short_line_is_still_padded(self):
        """折り返していない行は、これまでどおり広げること。"""
        s = feed(Screen(24, 40), "short")
        s.set_size(24, 100)
        self.assertEqual(len(s.lines[0]), 100)

    def test_no_command_crashes_when_the_line_is_not_as_wide_as_the_screen(self):
        """行の長さと桁数が食い違っても、どの命令でも落ちないこと。

        窓を縮めても行を切らず、折り返しで続く行は広げても埋めないので、
        行は桁数より長いことも短いこともある。カーソルは桁数まで動ける
        ので、行の実際の長さを見ずに触ると IndexError でアプリごと落ちる。
        実際 ESC[1K と ESC[P で落ちていた。
        """
        finals = "@ABCDEFGHIJKLMPSTXZdfghlmnrst`"
        params = ("", "0", "1", "2", "5", "99", "1;99", "99;99")
        for label, setup, size in (
                ("短い行", "y" * 60, (24, 120)),      # 折り返し行を広げた
                ("長い行", "z" * 120, (24, 20))):     # 長い行を縮めた
            for col in (1, 40, 100, 120):
                for final in finals:
                    for param in params:
                        with self.subTest(kind=label, col=col,
                                          seq=param + final):
                            s = Screen(24, 40)
                            p = Parser()
                            s.apply(p.feed(setup))
                            s.set_size(*size)
                            s.apply(p.feed("\x1b[1;%dH" % col))
                            s.apply(p.feed("\x1b[" + param + final))
                            s.apply(p.feed("X"))    # 続けて書けること

    def test_writing_past_the_end_of_a_wrapped_line_still_works(self):
        """埋めていない行の先へ書いても落ちないこと。"""
        s = Screen(24, 40)
        feed(s, "y" * 60)               # row0 は 40 セルのまま折り返す
        s.set_size(24, 100)
        feed(s, "\x1b[1;90HZ")          # 90 桁目へ書く
        self.assertEqual(s.text()[0][89], "Z")

    def test_a_real_line_break_is_never_joined(self):
        """機器が送った改行は、広げても繋がないこと。"""
        s = Screen(24, 40)
        feed(s, "first\r\nsecond")
        s.set_size(24, 120)
        self.assertEqual(s.text()[:2], ["first", "second"])

    def test_columns_survive_a_round_trip(self):
        """桁で並んだ出力 (ls の多段組) が往復で崩れないこと。"""
        rows = ["a.conf".ljust(20) + "b.conf".ljust(20) + "c.conf",
                "d.conf".ljust(20) + "e.conf".ljust(20) + "f.conf"]
        s = Screen(24, 80)
        feed(s, "\r\n".join(rows))
        before = s.text()
        s.set_size(24, 30)
        s.set_size(24, 80)
        self.assertEqual(s.text(), before)

    def test_the_screen_behind_an_alternate_screen_is_not_lost(self):
        """vi を開いている間に窓を縮めても、裏のシェル画面を捨てないこと。"""
        s = feed(Screen(), "\r\n".join("shell-%02d" % i for i in range(24)))
        feed(s, "\x1b[?1049h")
        s.set_size(10, 80)
        feed(s, "\x1b[?1049l")
        seen = everything(s)
        for i in range(24):
            self.assertIn("shell-%02d" % i, seen)

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


class RecordGapTest(unittest.TestCase):
    """画面から消えた行が記録に残らない経路の検証。"""

    def test_erase_above_on_a_wide_character_keeps_the_record(self):
        # カーソルが全角の前半桁にあると、ED 1 はその全角の継続セル
        # まで払うので画面は丸ごと空白になる。消える中身は ED 2 と
        # 同じなので、履歴にも同じだけ残る
        s = feed(Screen(rows=3, cols=10), "あい")
        feed(s, "\x1b[1;3H\x1b[1J")
        self.assertEqual(s.text(), ["", "", ""])
        self.assertIn("あい", everything(s))

    def test_deleting_the_top_line_of_the_screen_keeps_the_record(self):
        # 画面先頭の DL は上へ押し出す動きで、SU (CSI S) と同じ。
        # 消えた行は履歴へ送る
        s = feed(Screen(rows=3, cols=10), "one\r\ntwo\r\nthree")
        feed(s, "\x1b[1;1H\x1b[M")
        self.assertEqual(s.text(), ["two", "three", ""])
        self.assertIn("one", everything(s))

    def test_deleting_a_line_below_the_top_is_not_recorded(self):
        # カーソルより上の行は画面に残る。ここで記録すると同じ行が
        # 画面と履歴の両方に二重に出る
        s = feed(Screen(rows=3, cols=10), "one\r\ntwo\r\nthree")
        feed(s, "\x1b[2;1H\x1b[M")
        self.assertEqual(s.text(), ["one", "three", ""])
        self.assertEqual(len(s.history), 0)

    def test_deleting_the_top_line_of_a_scroll_region_is_not_recorded(self):
        # スクロール範囲の上端が画面の先頭でないときは、押し出された
        # 行は画面上に残っている (_scroll_up と同じ条件)
        s = feed(Screen(rows=4, cols=10), "one\r\ntwo\r\nthree\r\nfour")
        feed(s, "\x1b[2;4r\x1b[2;1H\x1b[M")
        self.assertEqual(s.text(), ["one", "three", "four", ""])
        self.assertEqual(len(s.history), 0)

    def test_deleting_the_top_line_of_the_alt_screen_is_not_recorded(self):
        # 代替画面の中身は記録しない方針 (vi 等はアプリが描き直す)
        s = feed(Screen(rows=3, cols=10), "\x1b[?1049halt\r\nnext")
        feed(s, "\x1b[1;1H\x1b[M")
        self.assertEqual(len(s.history), 0)


class AltScreenCharsetTest(unittest.TestCase):
    """?1049 は DECSC 相当の保存・復元 (XTerm ctlseqs)。"""

    def test_leaving_the_alt_screen_restores_the_charset(self):
        # 代替画面が指示した G0 を持ち帰ると、以降の出力も記録も
        # 罫線文字に化け続ける
        s = feed(Screen(), "\x1b[?1049h\x1b(0\x1b[?1049llqk")
        self.assertEqual(s.text()[0], "lqk")

    def test_leaving_the_alt_screen_restores_the_shift_state(self):
        # SO で G1 を使っている状態も 1049 で行き来する
        s = feed(Screen(), "\x1b)0\x1b[?1049h\x0e\x1b[?1049llqk")
        self.assertEqual(s.text()[0], "lqk")

    def test_entering_the_alt_screen_keeps_the_charset(self):
        # 保存はするが、入るときに指示を捨てはしない
        s = feed(Screen(), "\x1b(0\x1b[?1049hlqk")
        self.assertEqual(s.text()[0], "┌─┐")


if __name__ == "__main__":
    unittest.main()
