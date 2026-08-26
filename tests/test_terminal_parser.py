"""パーサ状態機械の検証。

Paul Williams の遷移図 (vt100.net/emu/dec_ansi_parser) の状態を
そのままテストにする。Qt を一切使わない。

いちばん大事なのは「受信がどこで切れても同じ命令列になる」こと。
旧実装はこれを正規表現の継ぎ足し (_pending_escape) でやっていて、
そこが不具合の温床だった。状態機械は状態を持ち越すだけでこれを
満たすはずで、実機の採取データ全部で確かめる。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import (      # noqa: E402
    Parser, Print, Ctrl, Esc, Csi, Osc, MAX_STRING)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures")


def parse(text):
    return Parser().feed(text)


def merged(events):
    """隣り合う Print を繋げる。切れ目の位置だけが違う出力を比べるため。"""
    result = []
    for event in events:
        if (result and isinstance(event, Print)
                and isinstance(result[-1], Print)):
            result[-1] = Print(result[-1].text + event.text)
        else:
            result.append(event)
    return result


def fixture(name):
    raw = io.open(os.path.join(FIXTURES, name), "rb").read()
    return raw.decode("utf-8", "replace")


class GroundStateTest(unittest.TestCase):
    def test_plain_text_comes_out_as_one_print(self):
        self.assertEqual(parse("show version"), [Print("show version")])

    def test_c0_controls_split_the_text(self):
        self.assertEqual(parse("ab\r\ncd"),
                         [Print("ab"), Ctrl("\r"), Ctrl("\n"), Print("cd")])

    def test_backspace_is_a_control(self):
        self.assertEqual(parse("a\bb"),
                         [Print("a"), Ctrl("\b"), Print("b")])

    def test_multibyte_text_prints(self):
        self.assertEqual(parse("日本語"), [Print("日本語")])

    def test_del_prints_here_and_is_dropped_by_the_screen(self):
        # 図では ground の 20-7F は print。捨てる判断は画面側の仕事
        self.assertEqual(parse("\x7f"), [Print("\x7f")])


class EscapeStateTest(unittest.TestCase):
    def test_a_final_byte_dispatches(self):
        self.assertEqual(parse("\x1b7"), [Esc("", "7")])

    def test_an_intermediate_is_collected(self):
        self.assertEqual(parse("\x1b(B"), [Esc("(", "B")])

    def test_keypad_mode_is_a_plain_escape(self):
        self.assertEqual(parse("\x1b="), [Esc("", "=")])

    def test_esc_inside_esc_starts_over(self):
        self.assertEqual(parse("\x1b\x1b7"), [Esc("", "7")])

    def test_a_control_executes_without_leaving_the_sequence(self):
        self.assertEqual(parse("\x1b(\rB"),
                         [Ctrl("\r"), Esc("(", "B")])

    def test_a_character_outside_the_diagram_aborts_and_prints(self):
        self.assertEqual(parse("\x1b日X"), [Print("日X")])


class CsiSequenceTest(unittest.TestCase):
    def test_no_params_at_all(self):
        self.assertEqual(parse("\x1b[m"), [Csi("", (), "", "m")])

    def test_params_are_numbers(self):
        self.assertEqual(parse("\x1b[1;24r"), [Csi("", (1, 24), "", "r")])

    def test_an_omitted_param_stays_visible_as_none(self):
        self.assertEqual(parse("\x1b[;5H"), [Csi("", (None, 5), "", "H")])

    def test_private_markers_are_kept_apart(self):
        self.assertEqual(parse("\x1b[?1049h"), [Csi("?", (1049,), "", "h")])

    def test_an_intermediate_is_collected(self):
        self.assertEqual(parse("\x1b[4 q"), [Csi("", (4,), " ", "q")])

    def test_an_unknown_final_still_comes_through(self):
        # 何を捨てるかを決めるのはパーサでなく画面
        self.assertEqual(parse("\x1b[999X"), [Csi("", (999,), "", "X")])

    def test_a_control_executes_without_leaving_the_sequence(self):
        self.assertEqual(parse("\x1b[1;\r24r"),
                         [Ctrl("\r"), Csi("", (1, 24), "", "r")])

    def test_a_colon_poisons_the_whole_sequence(self):
        # 図の通り。サブパラメータ (38:5:1 形式) は列ごと無視する
        self.assertEqual(parse("\x1b[38:5:1mX"), [Print("X")])

    def test_runaway_params_poison_the_whole_sequence(self):
        self.assertEqual(parse("\x1b[" + "1" * 300 + "mX"), [Print("X")])

    def test_cancel_aborts_midway(self):
        self.assertEqual(parse("\x1b[12\x18mX"),
                         [Ctrl("\x18"), Print("mX")])


class OscStringTest(unittest.TestCase):
    def test_bel_terminates_like_xterm(self):
        self.assertEqual(parse("\x1b]0;title\x07"), [Osc("0;title")])

    def test_st_terminates_like_the_diagram(self):
        self.assertEqual(parse("\x1b]0;hi\x1b\\"),
                         [Osc("0;hi"), Esc("", "\\")])

    def test_cancel_discards_the_partial_string(self):
        self.assertEqual(parse("\x1b]0;abc\x18Z"),
                         [Ctrl("\x18"), Print("Z")])

    def test_an_endless_string_is_capped(self):
        events = parse("\x1b]" + "a" * (MAX_STRING + 100) + "\x07")
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].text), MAX_STRING)


class SosPmApcTest(unittest.TestCase):
    def test_the_string_is_consumed_silently(self):
        self.assertEqual(parse("\x1bXsecret\x1b\\A"),
                         [Esc("", "\\"), Print("A")])


class StreamContinuityTest(unittest.TestCase):
    """受信の切れ目に関する性質。ここが旧実装の弱点だった。"""

    def test_state_survives_between_feeds(self):
        p = Parser()
        self.assertEqual(p.feed("\x1b("), [])
        self.assertEqual(p.feed("B"), [Esc("(", "B")])

    def test_every_split_point_gives_the_same_commands(self):
        stream = "ab\x1b(B\r\n\x1b]0;t\x07日\x1b7xy"
        whole = merged(parse(stream))
        for cut in range(1, len(stream)):
            p = Parser()
            split = merged(p.feed(stream[:cut]) + p.feed(stream[cut:]))
            self.assertEqual(split, whole,
                             "%d 文字目で切ると命令列が変わる" % cut)


class RealDeviceCaptureTest(unittest.TestCase):
    """Catalyst 8000v の実データ (7058 バイト)。"""

    def test_a_network_device_yields_only_print_and_ctrl(self):
        events = parse(fixture("cisco_ios_vt100.bin"))
        kinds = {type(e).__name__ for e in events}
        self.assertLessEqual(kinds, {"Print", "Ctrl"},
                             "機器の出力から想定外の命令が出た: %s" % kinds)

    def test_the_device_controls_are_just_cr_lf_bs(self):
        events = parse(fixture("cisco_ios_vt100.bin"))
        chars = {e.char for e in events if isinstance(e, Ctrl)}
        self.assertLessEqual(chars, {"\r", "\n", "\b"})

    def test_nothing_is_lost_or_invented(self):
        raw = fixture("cisco_ios_vt100.bin")
        events = parse(raw)
        rebuilt = "".join(
            e.text if isinstance(e, Print) else e.char for e in events)
        self.assertEqual(rebuilt, raw)

    def test_every_split_point_gives_the_same_commands(self):
        raw = fixture("cisco_ios_vt100.bin")
        whole = merged(parse(raw))
        for cut in range(1, len(raw), 7):
            p = Parser()
            split = merged(p.feed(raw[:cut]) + p.feed(raw[cut:]))
            self.assertEqual(split, whole,
                             "%d バイト目で切ると命令列が変わる" % cut)


class RealLinuxCaptureTest(unittest.TestCase):
    """Ubuntu の実データ (nano / 普通のシェル / clear)。

    値はどれも実測。nano は TERM=vt100 では代替画面 (?1049) を使わず、
    DECSTBM (ESC[1;24r) と CUP で全画面を組む。clear は ESC[H ESC[J。
    どのシェル操作にも OSC は 1 度も現れない。
    """
    LINUX = ("nano_vt100.bin", "shell_vt100.bin", "clear_vt100.bin")

    def test_nano_reaches_the_full_screen_markers(self):
        events = parse(fixture("nano_vt100.bin"))
        csi = [(e.private, e.params, e.final)
               for e in events if isinstance(e, Csi)]
        self.assertIn(("", (1, 24), "r"), csi)      # DECSTBM
        self.assertIn(("", (22, 16), "H"), csi)     # CUP 行桁指定

    def test_bash_toggles_bracketed_paste(self):
        events = parse(fixture("shell_vt100.bin"))
        csi = [(e.private, e.params, e.final)
               for e in events if isinstance(e, Csi)]
        self.assertIn(("?", (2004,), "h"), csi)
        self.assertIn(("?", (2004,), "l"), csi)

    def test_clear_erases_instead_of_scrolling(self):
        events = parse(fixture("clear_vt100.bin"))
        csi = [(e.private, e.params, e.final)
               for e in events if isinstance(e, Csi)]
        self.assertIn(("", (), "H"), csi)
        self.assertIn(("", (), "J"), csi)

    def test_no_escape_ever_leaks_into_printed_text(self):
        for name in self.LINUX:
            for event in parse(fixture(name)):
                if isinstance(event, Print):
                    self.assertNotIn("\x1b", event.text, name)

    def test_every_split_point_gives_the_same_commands(self):
        for name in self.LINUX:
            raw = fixture(name)
            whole = merged(parse(raw))
            for cut in range(1, len(raw)):
                p = Parser()
                split = merged(p.feed(raw[:cut]) + p.feed(raw[cut:]))
                self.assertEqual(
                    split, whole,
                    "%s: %d バイト目で切ると命令列が変わる" % (name, cut))


if __name__ == "__main__":
    unittest.main()
