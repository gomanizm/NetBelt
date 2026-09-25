"""印字と CSI の近道が、1 文字ずつの処理と同じ結果になることの検証。

大量出力を速く捌くため、パーサは GROUND の印字の連なりと素直な CSI を
まとめて処理し、画面は幅 1 の文字の連なりを行ごとにまとめて書き込む。
どちらも 1 文字ずつ処理したときと 1 つも違ってはいけない (命令列の
切れ目・パーサの状態・折り返しの印・折り返し待ち・全角の片割れの始末・
履歴まで)。種を固定した乱数の受信列で、近道を通る側と通らない側を
突き合わせる。Qt を使わない。
"""
import io
import os
import random
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core.terminal import parser as parser_module   # noqa: E402
from core.terminal.parser import Parser, Print, Ctrl  # noqa: E402
from core.terminal.screen import Screen             # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures")

ESC = chr(0x1B)
ASCII = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
         "0123456789 !#$%&()*+,-./:;<=>?@[]^_`{|}~'\"\\")
# 全角・結合文字と書式文字 (幅 0)・DEL・C1・U+00AD 前後・罫線・絵文字
OTHER = ("漢字ｱ가" + chr(0x3000) + chr(0x301) + chr(0x200D) + chr(0xFE0F)
         + chr(0x7F) + chr(0x85) + chr(0x9B) + chr(0xAC) + chr(0xAD)
         + chr(0xAE) + chr(0xE9) + chr(0x2500) + chr(0x1F600))
COMMANDS = [
    "\r", "\n", "\r\n", "\t", "\b", "\x07", "\x00", "\x0e", "\x0f",
    "\x18", "\x1a",
    ESC + "[m", ESC + "[1;31m", ESC + "[38;5;200;48;2;1;2;3m", ESC + "[0m",
    ESC + "[?7l", ESC + "[?7h", ESC + "[4h", ESC + "[4l",
    ESC + "[2;3r", ESC + "[r", ESC + "[?1049h", ESC + "[?1049l",
    ESC + "[?47h", ESC + "[?47l", ESC + "(0", ESC + "(B", ESC + ")0",
    ESC + "7", ESC + "8", ESC + "M", ESC + "D", ESC + "E",
    ESC + "[H", ESC + "[3;4H", ESC + "[;7f", ESC + "[2A", ESC + "[5G",
    ESC + "[K", ESC + "[1K", ESC + "[J", ESC + "[2@", ESC + "[3P",
    ESC + "[2X", ESC + "[L", ESC + "[M", ESC + "[S", ESC + "[T",
    ESC + "[6n", ESC + "[c", ESC + "[?25l", ESC + "[>0c", ESC + "[=5m",
    ESC + "[<<m", ESC + "[??25h", ESC + "[1 q", ESC + "[1;2\x07m",
    ESC + "[1;2" + chr(0x7F) + "m", ESC + "[1" + chr(0xE9),
    ESC + "[:3m", ESC + "[1:3m", ESC + "[?1?h",
    ESC + "[" + "1" * 256 + "m", ESC + "[" + "1" * 257 + "m",
    ESC + "]0;title\x07", ESC + "P1q" + ESC, ESC + "[", ESC,
]
NEVER = re.compile(r"(?!)")


def random_stream(rnd):
    parts = []
    for _ in range(rnd.randint(1, 25)):
        k = rnd.random()
        if k < 0.45:
            parts.append("".join(rnd.choice(ASCII)
                                 for _ in range(rnd.randint(1, 60))))
        elif k < 0.6:
            parts.append("".join(rnd.choice(ASCII + OTHER)
                                 for _ in range(rnd.randint(1, 30))))
        else:
            parts.append(rnd.choice(COMMANDS))
    return "".join(parts)


def random_pieces(rnd, stream):
    """受信の切れ目を乱数の位置に 3 つ入れる (空の片も出る)。"""
    cuts = sorted(rnd.choice(range(len(stream) + 1)) for _ in range(3))
    return [stream[a:b] for a, b in zip([0] + cuts, cuts + [len(stream)])]


def state_machine_feed(parser, text):
    """近道を止め、1 文字ずつの状態機械だけで食わせる。"""
    with mock.patch.object(parser_module, "_PRINTABLE_RUN", NEVER), \
            mock.patch.object(parser_module, "_SIMPLE_CSI", NEVER):
        return parser.feed(text)


def parser_state(p):
    return (p.state, p._private, p._params, p._intermediate,
            list(p._string), p._hook)


class PerCharScreen(Screen):
    """印字を常に 1 文字ずつ書く画面 (近道を通らない基準)。"""

    def _print(self, text):
        self._print_chars(text, None, False)


def screen_state(s):
    return (s.lines, s._other, s.wrapped, s._other_wrapped,
            (s.cursor_row, s.cursor_col, s._pending_wrap, s.attr),
            list(s.history), s.take_new_history(), s.take_dirty(),
            s.take_responses())


class ParserFastPathTest(unittest.TestCase):
    def test_same_events_and_state_as_the_state_machine_alone(self):
        rnd = random.Random(20260918)
        for case in range(1500):
            stream = random_stream(rnd)
            fast, slow = Parser(), Parser()
            for piece in random_pieces(rnd, stream):
                with self.subTest(case=case, piece=piece):
                    # Print の切れ目まで同じ (受け取る側はそれを前提にする)
                    self.assertEqual(fast.feed(piece),
                                     state_machine_feed(slow, piece))
                    self.assertEqual(parser_state(fast), parser_state(slow))

    def test_captured_device_output(self):
        for name in sorted(os.listdir(FIXTURES)):
            if not name.endswith(".bin"):
                continue
            with io.open(os.path.join(FIXTURES, name), "rb") as f:
                text = f.read().decode("utf-8", "replace")
            for size in (len(text), 997, 61):
                fast, slow = Parser(), Parser()
                for i in range(0, len(text), size):
                    piece = text[i:i + size]
                    with self.subTest(name=name, size=size, at=i):
                        self.assertEqual(fast.feed(piece),
                                         state_machine_feed(slow, piece))
                        self.assertEqual(parser_state(fast),
                                         parser_state(slow))

    def test_every_c0_control_between_text(self):
        for code in range(0x20):
            if code == 0x1B:
                continue
            with self.subTest(code=code):
                self.assertEqual(Parser().feed("a" + chr(code) + "b"),
                                 [Print("a"), Ctrl(chr(code)), Print("b")])


class ScreenFastPathTest(unittest.TestCase):
    def check(self, rows, cols, pieces, resize=None):
        fast, slow = Screen(rows, cols, 50), PerCharScreen(rows, cols, 50)
        p = Parser()
        for n, piece in enumerate(pieces):
            events = p.feed(piece)
            fast.apply(events)
            slow.apply(events)
            if resize and n == len(pieces) // 2:
                fast.set_size(*resize)
                slow.set_size(*resize)
            self.assertEqual(screen_state(fast), screen_state(slow))

    def test_random_streams_on_small_screens(self):
        rnd = random.Random(1918)
        for case in range(1500):
            rows, cols = rnd.randint(1, 8), rnd.randint(1, 14)
            stream = random_stream(rnd)
            resize = ((rnd.randint(1, 8), rnd.randint(1, 14))
                      if rnd.random() < 0.2 else None)
            with self.subTest(case=case, size=(rows, cols), stream=stream):
                self.check(rows, cols, random_pieces(rnd, stream), resize)

    def test_lines_ending_exactly_at_the_right_edge(self):
        # 行幅ちょうどで終わる印字、折り返し済みの行への書き直し、
        # スクロール範囲の下端での折り返し
        rnd = random.Random(7)
        for case in range(800):
            rows, cols = rnd.randint(2, 5), rnd.randint(1, 8)
            pieces = []
            for _ in range(rnd.randint(2, 8)):
                k = rnd.random()
                if k < 0.2:
                    pieces.append(ESC + "[%d;%dr" % (rnd.randint(1, rows),
                                                     rnd.randint(1, rows)))
                elif k < 0.45:
                    pieces.append(ESC + "[%d;%dH" % (rnd.randint(1, rows),
                                                     rnd.randint(1, cols)))
                elif k < 0.5:
                    pieces.append(ESC + "[?7" + rnd.choice("hl"))
                else:
                    n = rnd.choice([cols, 2 * cols, 3 * cols, cols - 1,
                                    cols + 1, 1]) or 1
                    pieces.append("".join(rnd.choice(ASCII + "漢" + chr(0x301))
                                          for _ in range(n)))
            with self.subTest(case=case, size=(rows, cols), pieces=pieces):
                self.check(rows, cols, pieces)

    def test_captured_device_output(self):
        for name in sorted(os.listdir(FIXTURES)):
            if not name.endswith(".bin"):
                continue
            with io.open(os.path.join(FIXTURES, name), "rb") as f:
                text = f.read().decode("utf-8", "replace")
            for rows, cols in ((24, 80), (10, 20), (3, 2)):
                with self.subTest(name=name, size=(rows, cols)):
                    self.check(rows, cols,
                               [text[i:i + 997]
                                for i in range(0, len(text), 997)])


if __name__ == "__main__":
    unittest.main()
