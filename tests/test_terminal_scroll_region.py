"""スクロール範囲 (DECSTBM) の固着と、それによる記録の欠落を検証する。

見つかった不具合は2つある。どちらか片方を直しても、もう片方は残る。

1. 画面より大きい範囲指定を丸めずに拒否していた。
   xterm は下端を画面行数へ丸めて受理する。丸めずに捨てると、直前に
   受理した狭い範囲がそのまま残り続ける。ncurses は部分スクロールの
   最適化で小さい範囲を設定し、最後に全画面へ戻すが、その「戻す」側の
   行数はアプリが信じている行数（多くは 24）なので、画面がそれより
   小さいと復帰だけが拒否されて狭い範囲が固着する。
   tmux のペイン、上部にダイアログを出すアプリなどで踏む。

2. 履歴へ送る条件が厳しすぎた。
   `scroll_top == 0 and scroll_bottom == rows - 1` の両方を求めていたが、
   xterm が記録するかどうかは上端が画面の先頭かどうかで決まる。
   端末が 30 行あり、機器が 24 行だと信じていると `ESC[1;24r` は
   そのまま受理されるので、条件 1 を直しても scroll_bottom は 23 の
   まま残り、以後の出力が記録されなくなる。窓の大きさを機器へ伝えるのは
   SSH だけなので、Telnet・シリアルでは普通に起こる。

固着した範囲は、押し出された行が history へ入らないまま消える。
利用者から見ると「出力が画面の一部の帯だけで流れ、スクロールバックにも
残っていない」という形になる。窓をリサイズすると直るが、その関係は見えない。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def lines(n, prefix="row"):
    return "\r\n".join("%s%02d" % (prefix, i) for i in range(n))


class ClampingTest(unittest.TestCase):
    """画面より大きい範囲指定は、捨てずに丸めて受理する。"""

    def test_an_oversized_bottom_margin_is_clamped_to_the_screen(self):
        """下端だけがはみ出した指定は、上端を保ったまま丸めること。

        上端を 0 以外にしてあるのは、丸めた結果と既定値 (0, rows-1) を
        取り違えないため。拒否されると (0, 19) のまま残る。
        """
        s = feed(Screen(rows=20, cols=80), "\x1b[5;40r")
        self.assertEqual((s.scroll_top, s.scroll_bottom), (4, 19),
                         "画面より大きい下端が丸められていない")

    def test_an_oversized_reset_releases_a_narrow_region(self):
        """狭い範囲を設定したあと、大きすぎる全画面復帰で解放されること。

        ncurses が csr(0,9) → csr(0,23) と打つ形。復帰が拒否されると
        狭い範囲が残ったままになる。
        """
        s = feed(Screen(rows=20, cols=80), "\x1b[1;10r")
        self.assertEqual((s.scroll_top, s.scroll_bottom), (0, 9),
                         "画面に収まる範囲が受理されていない")

        feed(s, "\x1b[1;24r")
        self.assertEqual((s.scroll_top, s.scroll_bottom), (0, 19),
                         "全画面への復帰が拒否されて狭い範囲が固着している")

    def test_history_resumes_after_an_oversized_reset(self):
        """固着が解ければ、押し出された行がまた記録されること。"""
        s = feed(Screen(rows=20, cols=80), "\x1b[1;10r\x1b[1;24r")
        feed(s, lines(30))
        self.assertGreater(len(s.history), 0,
                           "全画面へ戻したのに記録が止まったまま")

    def test_margins_that_cannot_be_clamped_into_shape_are_refused(self):
        """丸めても上端が下端に追いつかない指定は、これまでどおり拒否。"""
        s = feed(Screen(rows=20, cols=80), "\x1b[30;40r")
        self.assertEqual((s.scroll_top, s.scroll_bottom), (0, 19),
                         "成立しない範囲を受け入れている")

    def test_an_inverted_range_is_still_refused(self):
        """上下が逆の指定は丸めても受け付けないこと。"""
        s = feed(Screen(rows=24, cols=80), "\x1b[7;3r")
        self.assertEqual((s.scroll_top, s.scroll_bottom), (0, 23))


class ZeroMeansDefaultTest(unittest.TestCase):
    """DECSTBM の 0 は「既定値」(xterm と同じ)。

    下端に 0 を送ると 0-1 = -1 が 0 へ丸められ、上端 < 下端 を満たさず
    黙って拒否されていた。直前の狭い範囲がそのまま残り、押し出された
    行は履歴にも入らない。
    """

    def test_zero_zero_restores_the_full_screen(self):
        s = feed(Screen(rows=24, cols=80), "\x1b[2;3r")
        self.assertEqual((s.scroll_top, s.scroll_bottom), (1, 2))
        feed(s, "\x1b[0;0r")
        self.assertEqual((s.scroll_top, s.scroll_bottom), (0, 23),
                         "ESC[0;0r で全画面へ戻っていない")

    def test_a_zero_bottom_means_the_last_row(self):
        s = feed(Screen(rows=24, cols=80), "\x1b[2;3r")
        feed(s, "\x1b[1;0r")
        self.assertEqual((s.scroll_top, s.scroll_bottom), (0, 23),
                         "下端 0 が最終行として扱われていない")

    def test_history_resumes_after_a_zero_reset(self):
        s = feed(Screen(rows=24, cols=80), "\x1b[2;3r\x1b[0;0r")
        feed(s, lines(30))
        self.assertGreater(len(s.history), 0,
                           "全画面へ戻したのに記録が止まったまま")


class HistoryFromATopAnchoredRegionTest(unittest.TestCase):
    """上端が画面の先頭なら、押し出された行は記録する。"""

    def test_lines_leaving_a_top_anchored_region_reach_the_history(self):
        """30 行の端末で機器が 24 行と信じていても記録が続くこと。

        ESC[1;24r は 30 行の画面では条件を満たすのでそのまま受理される。
        丸めの修正では届かないのがこの経路で、しかも端末を広げている
        普通の状態で踏む。
        """
        s = feed(Screen(rows=30, cols=80), "\x1b[1;24r")
        feed(s, lines(50))
        self.assertGreater(len(s.history), 0,
                           "上端が先頭の範囲なのに記録されていない")

    def test_the_recorded_lines_are_the_ones_that_left_the_top(self):
        """記録された内容が、実際に押し出された行であること。"""
        s = feed(Screen(rows=30, cols=80), "\x1b[1;24r")
        feed(s, lines(50))
        first = "".join(cell[0] for cell in s.history[0]).rstrip()
        self.assertEqual(first, "row00",
                         "記録された行が押し出された行と違う")

    def test_lines_leaving_a_region_below_the_top_are_not_recorded(self):
        """上端が先頭でない範囲では記録しないこと。

        この場合、上端から出た行は画面から消えるわけではなく、
        範囲の外に残っている行がそのまま見えている。
        """
        s = feed(Screen(rows=24, cols=80), "\x1b[2;3r\x1b[3;1H")
        feed(s, lines(10))
        self.assertEqual(len(s.history), 0,
                         "画面に残っている行を記録している")

    def test_the_alt_screen_still_never_reaches_history(self):
        """代替画面のスクロールは、範囲がどうであれ記録しないこと。"""
        s = feed(Screen(rows=24, cols=80), "\x1b[?1049h\x1b[1;20r")
        feed(s, lines(40))
        self.assertEqual(len(s.history), 0,
                         "代替画面の内容が記録に混ざっている")


class NextAndPreviousLineTest(unittest.TestCase):
    """CNL (CSI E) / CPL (CSI F) も範囲で止まること。

    xterm の CursorNextLine / CursorPrevLine は CursorDown / CursorUp を
    通るので、CUD / CUU と同じ頭打ちが効く。
    """

    def test_cnl_stops_at_the_bottom_of_the_region(self):
        s = feed(Screen(rows=24, cols=80), "\x1b[5;10r\x1b[8;3H\x1b[20E")
        self.assertEqual((s.cursor_row, s.cursor_col), (9, 0))

    def test_cpl_stops_at_the_top_of_the_region(self):
        s = feed(Screen(rows=24, cols=80), "\x1b[5;10r\x1b[8;3H\x1b[20F")
        self.assertEqual((s.cursor_row, s.cursor_col), (4, 0))

    def test_cnl_from_below_the_region_still_stops_at_the_screen(self):
        # 範囲の外にいるカーソルは、CUD と同じく画面の端まで動ける
        s = feed(Screen(rows=24, cols=80), "\x1b[5;10r\x1b[12;3H\x1b[20E")
        self.assertEqual((s.cursor_row, s.cursor_col), (23, 0))

    def test_cpl_from_above_the_region_still_stops_at_the_screen(self):
        s = feed(Screen(rows=24, cols=80), "\x1b[5;10r\x1b[2;3H\x1b[20F")
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 0))


if __name__ == "__main__":
    unittest.main()
