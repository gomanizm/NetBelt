r"""履歴の差分の上限を「行」で数えると、折り返しが混ざった文書が
上限に届かずに古い行が消える件を検証する。

差分の上限 (Screen.MAX_NEW_HISTORY) は、描画側が文書に残す行数
(TerminalWidget.MAX_DOCUMENT_BLOCKS = 20,000) と同じ数に取ってある。
「上限以上なら、文書は上限が無かったときと同じになる」という前提は、
差分の 1 行が文書の 1 行になるときしか成り立たない。折り返しで続く
行 (wrapped) は次の行と繋いで 1 行として書かれるので、差分 20,000 行
から出来る文書は 20,000 行より短くなり、そのぶん文書の先頭が足りない。

実測 (ESC[nS を 860 回流して履歴を 20,640 行押し出したあと、画面の桁
ちょうどで折り返す 'W' の連なりを 1 描画単位 16,384 文字に収めて流し、
TerminalWidget の文書を比べた):
  24x20 : 上限なし 20,000 行 / 32,079 文字 -> 行で数える上限 19,445 行
  24x80 : 上限なし 20,000 行 / 32,079 文字 -> 行で数える上限 19,898 行
  40x20 : 上限なし 20,000 行 / 33,379 文字 -> 行で数える上限 19,412 行
足りない分 (最大 588 行) は文書のいちばん古い側なので、上へ戻って
過去の出力を読むと、上限が無かったときには見えていた行が消えている。

直し方: 数えるのを「行」から「文書の 1 行を終える行 (折り返しで続か
ない行)」へ変える。差分に残す論理行の数を MAX_NEW_HISTORY にすれば、
折り返しが何行挟まっても文書は必ず上限行ぶん埋まるので、描き終えた
文書は上限が無かったときと同じになる。捨てるときは論理行の頭まで
まとめて捨てる (途中から残すと、切れ端が 1 行として文書へ入る)。
ESC[nS / ESC[nM の洪水は折り返しの印が付かない空行なので、抑えの
効き方はこれまでどおり (差分は 20,000 行で頭打ち)。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
OUTPUT_SLICE = 16384


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def worst_case(rows, cols, reps):
    """履歴を洪水で押し出したあと、桁ちょうどで折り返す出力を続ける。"""
    flood = (ESC + "[%dS" % rows) * reps
    body = "W" * ((OUTPUT_SLICE - len(flood) - 2) // cols * cols)
    return flood + body


def logical_lines(delta):
    """差分を描画側と同じ繋ぎ方 (折り返しは次と繋ぐ) で論理行へ戻す。"""
    out, buf = [], ""
    for line, wrapped in delta:
        buf += "".join(cell[0] for cell in line)
        if not wrapped:
            out.append(buf.rstrip())
            buf = ""
    if buf:
        out.append(buf.rstrip())
    return out


class WrappedDeltaCapTest(unittest.TestCase):
    def test_the_cap_counts_logical_lines_not_rows(self):
        """折り返しを挟んでも、論理行が上限ぶん残ること。"""
        s = feed(Screen(24, 20), worst_case(24, 20, 860))
        delta = s.take_new_history()
        self.assertEqual(sum(1 for _, wrapped in delta if not wrapped),
                         Screen.MAX_NEW_HISTORY,
                         "文書を埋めるだけの論理行が残っていない")

    def test_a_wrapped_flood_keeps_whole_logical_lines(self):
        """1 行が 2 行に折り返す出力でも、切れ端を残さないこと。"""
        count = Screen.MAX_NEW_HISTORY + 50
        text = "".join(("L%05d" % i).ljust(12, ".") + "\r\n"
                       for i in range(count))
        s = feed(Screen(3, 8), text)
        delta = s.take_new_history()
        self.assertTrue(s.take_history_dropped(), "捨てたのに伝わらない")
        self.assertEqual(sum(1 for _, wrapped in delta if not wrapped),
                         Screen.MAX_NEW_HISTORY,
                         "論理行が上限ぶん残っていない")
        self.assertGreater(len(delta), Screen.MAX_NEW_HISTORY,
                           "折り返しの行まで上限に数えている")
        lines = logical_lines(delta)
        head = int(lines[0][1:6])
        self.assertEqual(lines,
                         [("L%05d" % i).ljust(12, ".")
                          for i in range(head, head + len(lines))],
                         "論理行の途中から残っている (切れ端が混ざる)")

    def test_a_scroll_up_flood_is_still_capped_by_rows(self):
        """折り返しの無い洪水では、これまでどおり行数で頭打ちになること。"""
        s = feed(Screen(24, 80), (ESC + "[24S") * 1000)
        self.assertEqual(len(s.take_new_history()), Screen.MAX_NEW_HISTORY)


class WrappedDeltaCapThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def document_for(self, rows, cols, text, cap=None):
        """その入力を描き切ったあとの文書を返す。cap で上限を差し替える。"""
        from ui.terminal_widget import TerminalWidget
        kept = Screen.MAX_NEW_HISTORY
        if cap is not None:
            Screen.MAX_NEW_HISTORY = cap
        try:
            w = TerminalWidget()
            self.addCleanup(w.close)
            terminal = w.create_terminal_tab("dev")
            terminal._screen = Screen(rows, cols)
            w.queue_output("dev", text)
            while w._pending_output:
                w._flush_pending_output()
            return terminal.toPlainText(), terminal.document().blockCount()
        finally:
            Screen.MAX_NEW_HISTORY = kept

    def test_the_document_matches_the_run_without_a_cap(self):
        """折り返しが混ざっても、文書が上限の有無で変わらないこと。"""
        for rows, cols, reps in ((24, 20, 860), (24, 80, 860), (40, 20, 600)):
            with self.subTest(rows=rows, cols=cols, reps=reps):
                text = worst_case(rows, cols, reps)
                capped = self.document_for(rows, cols, text)
                uncapped = self.document_for(rows, cols, text, cap=10 ** 9)
                self.assertEqual(capped[1], uncapped[1],
                                 "上限で文書の行数が変わっている")
                self.assertEqual(capped[0], uncapped[0],
                                 "上限で文書の中身が変わっている")

    def test_the_document_still_fills_its_limit(self):
        """洪水のあとでも、文書は残せるだけ残すこと。"""
        from ui.terminal_widget import TerminalWidget
        text = worst_case(24, 20, 860)
        self.assertEqual(self.document_for(24, 20, text)[1],
                         TerminalWidget.MAX_DOCUMENT_BLOCKS,
                         "文書が上限に届いていない")


if __name__ == "__main__":
    unittest.main()
