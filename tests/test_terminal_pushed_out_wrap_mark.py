r"""スクロール範囲の高さぶん巻き上げたとき、履歴へ押し出される行の
折り返しの印が古いまま残る件を検証する。

範囲の下端にあった行の続きは、範囲の 1 つ下の行 (範囲の外) にある。
SU / DL が範囲の高さぶん回すと、その行は上がりきって範囲から出て
履歴へ入るが、続きは画面に残ったままなので印が古くなる。描画側は
印の付いた行を次の行と繋いで書くため、履歴のその行は、あとから空いた
先頭へ来た無関係な出力と 1 行に繋がる。

実測 (7 周目の途中の版。Screen(4, 4)、範囲 1-3 = 0..2、行 2 の
'A0--' が範囲の外の行 3 'A1--' へ続いている状態):
  ESC[3S ESC[1;1H 'ZZZZ' -> 差分 [('',F), ('',F), ('A0--',True)]
  描画側と同じ繋ぎ方の文書 = ['', '', 'A0--ZZZZ', '', '', 'A1--']
  DL 版 (ESC[1;1H ESC[3M) も同じ。範囲の高さに届かない ESC[2S /
  ESC[2M では、上がった先で印を外す直しが既に効いていた。
2 手までの総当たり (5 通りの下地 x 38 命令の 2 手 = 7,220 場面) で
古い印が残る組み合わせを数えると、基準 16101ef が 15、途中の版が 3、
この直しで 0 になる。

直し方: 上がった先 (scroll_bottom - n) で外すのをやめ、行を動かす前に
範囲の下端の印を外す。印は行と一緒に動くので上がった先で外すのと同じ
結果になり、範囲から押し出されて履歴へ入る場合も印が落ちた状態で入る。
最下行での折り返し (_linefeed(from_wrap=True)) の印はそこに載っている
ので、その 1 経路だけ従来どおり残す。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
# 行 2 の 'A0--' が範囲の外の行 3 'A1--' へ続いている 4x4 の下地。
# 範囲は 1-3 行目 (0..2) で、最下行はステータス行のように範囲の外
SETUP = ESC + "[3;1H" + "A0--A1--" + ESC + "[1;3r"
FULL_SCROLL_UP = ESC + "[3S"
FULL_DELETE_LINE = ESC + "[1;1H" + ESC + "[3M"
AFTER = ESC + "[1;1H" + "ZZZZ"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def row_text(line):
    return "".join(cell[0] for cell in line).rstrip()


def staged(command):
    """下地を置いてから command を流し、そのあと先頭へ別の出力を出す。"""
    screen = feed(Screen(4, 4), SETUP)
    screen.take_new_history()
    return feed(screen, command + AFTER)


def joined(screen):
    """差分と画面を、描画側と同じ繋ぎ方 (折り返しは次と繋ぐ) で並べる。"""
    stream = list(screen.take_new_history())
    stream += [(line, screen.wrapped[r])
               for r, line in enumerate(screen.lines)]
    out, buf = [], ""
    for line, wrapped in stream:
        buf += ("".join(cell[0] for cell in line) if wrapped
                else row_text(line))
        if not wrapped:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


class PushedOutWrapMarkTest(unittest.TestCase):
    def test_scroll_up_by_the_region_height_drops_the_mark(self):
        """SU が範囲の高さぶん回ったら、押し出された行の印を外すこと。"""
        delta = staged(FULL_SCROLL_UP).take_new_history()
        self.assertEqual([(row_text(line), wrapped)
                          for line, wrapped in delta],
                         [("", False), ("", False), ("A0--", False)],
                         "続きを画面に置いたまま履歴へ入った行に印が残る")

    def test_delete_line_by_the_region_height_drops_the_mark(self):
        """DL が範囲の高さぶん回ったときも同じであること。"""
        delta = staged(FULL_DELETE_LINE).take_new_history()
        self.assertEqual([(row_text(line), wrapped)
                          for line, wrapped in delta],
                         [("", False), ("", False), ("A0--", False)],
                         "続きを画面に置いたまま履歴へ入った行に印が残る")

    def test_the_pushed_out_row_is_not_joined_to_later_output(self):
        """押し出された行が、あとから来た無関係な出力と繋がらないこと。"""
        for tag, command in (("SU", FULL_SCROLL_UP),
                             ("DL", FULL_DELETE_LINE)):
            with self.subTest(command=tag):
                lines = joined(staged(command))
                self.assertNotIn("A0--ZZZZ", lines,
                                 "無関係な 2 行が 1 行に繋がっている")
                self.assertIn("A0--", lines, "押し出された行が消えている")
                self.assertIn("ZZZZ", lines, "あとの出力が消えている")

    def test_a_plain_wrap_at_the_last_row_still_joins(self):
        """最下行での素の折り返しは、これまでどおり繋がること。"""
        screen = feed(Screen(2, 4), "ABCDEFGHIJ")
        self.assertEqual([(row_text(line), wrapped)
                          for line, wrapped in screen.take_new_history()],
                         [("ABCD", True)], "素の折り返しが切れている")
        self.assertEqual(screen.wrapped, [True, False])

    def test_a_wrap_at_the_region_bottom_still_joins(self):
        """範囲の下端での折り返しも、これまでどおり繋がること。"""
        screen = feed(Screen(4, 4),
                      ESC + "[1;3r" + ESC + "[1;1H" + "ABCDEFGHIJKLMN")
        self.assertEqual([(row_text(line), wrapped)
                          for line, wrapped in screen.take_new_history()],
                         [("ABCD", True)], "折り返しが切れている")
        self.assertEqual(screen.text(), ["EFGH", "IJKL", "MN", ""])
        self.assertEqual(screen.wrapped, [True, True, False, False])


class PushedOutWrapMarkThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def document_after(self, command):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        terminal._screen = Screen(4, 4)
        w.queue_output("dev", SETUP + command + AFTER)
        while w._pending_output:
            w._flush_pending_output()
        return [line.rstrip() for line in terminal.toPlainText().split("\n")]

    def test_the_document_keeps_the_two_lines_apart(self):
        """利用者が見る文書でも 1 行にならないこと。"""
        for tag, command in (("SU", FULL_SCROLL_UP),
                             ("DL", FULL_DELETE_LINE)):
            with self.subTest(command=tag):
                lines = self.document_after(command)
                self.assertNotIn("A0--ZZZZ", lines,
                                 "無関係な 2 行が 1 行に繋がっている")
                self.assertIn("A0--", lines, "押し出された行が消えている")


if __name__ == "__main__":
    unittest.main()
