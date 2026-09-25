r"""ESC[nS / ESC[nM の繰り返しで、描画側へ渡す履歴の差分が青天井に
伸びる件を検証する。

1 命令あたりの行数はスクロール範囲の高さで頭打ちになっているが、命令
の繰り返し回数には上限が無い。画面へ渡される履歴 (Screen.history) は
deque(maxlen=5000) で抑えられているのに、描画側へ渡す差分
(_new_history) は素の list で上限が無かった。

実測 (基準 16101ef、1 回の描画単位 OUTPUT_SLICE=16384 文字ぶんの入力):
   24x80  入力16380文字 ->  78,624行 /   6,289,920 セル参照 (約  50MB)
   50x200 入力16380文字 -> 163,800行 /  32,760,000 セル参照 (約 262MB)
  200x500 入力16380文字 -> 546,000行 / 273,000,000 セル参照 (約2184MB)
  200x500 の tracemalloc は current=peak=2249.9MB。
TerminalWidget を通した 1 回の描画は 24x80 で 0.40 秒、200x500 で 20.56
秒 GUI が止まった。文書は MAX_DOCUMENT_BLOCKS=20,000 行で頭打ちなので、
52 万行ぶんの確保と書き込みは最後に捨てられるためだけに行われていた。
履歴を作らない SD (ESC[nT) と IL (ESC[nL) は同じ入力で 0.44 秒しか
かからず、重さの主因が差分の抱え込みであることも確かめてある。

直し方: _new_history を上限付き (Screen.MAX_NEW_HISTORY) にして、
あふれた分は古い方から捨てる。上限は文書の上限 MAX_DOCUMENT_BLOCKS
以上にしてあり、文書は末尾から数えて上限行だけを残すので、描き終えた
文書は上限が無かったときと同じになる。数えるのは「行」ではなく
「文書の 1 行を終える行」で、折り返しが混ざったときに文書が上限へ
届かなくなる件は tests/test_terminal_history_delta_wrapped_lines.py
で別に見ている。捨てたことは
take_history_dropped() で描画側へ伝え、「全ログ保存」の欠落警告
(_log_truncated) を立てる。Screen.history は deque(maxlen) のまま触らない。
"""
import os
import sys
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


def document_limit():
    """描画側が文書に残す行数 (これを超える差分は作っても捨てられる)。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from ui.terminal_widget import TerminalWidget
    return TerminalWidget.MAX_DOCUMENT_BLOCKS


class HistoryDeltaCapTest(unittest.TestCase):
    def test_a_scroll_up_flood_does_not_grow_the_delta_without_end(self):
        """SU の繰り返しで、文書へ入りきらない行まで作らないこと。"""
        s = feed(Screen(24, 80), (ESC + "[24S") * 1000)
        self.assertLessEqual(len(s.take_new_history()), document_limit())

    def test_a_delete_line_flood_does_not_grow_the_delta_without_end(self):
        """DL の繰り返しでも、文書へ入りきらない行まで作らないこと。"""
        s = feed(Screen(24, 80), (ESC + "[24M") * 1000)
        self.assertLessEqual(len(s.take_new_history()), document_limit())

    def test_the_cap_is_at_least_the_document_limit(self):
        """上限が文書の上限以上で、文書の中身が変わらないこと。"""
        self.assertGreaterEqual(Screen.MAX_NEW_HISTORY, document_limit())

    def test_the_newest_rows_are_the_ones_kept(self):
        """捨てるのは古い方で、残った差分が画面と地続きであること。"""
        count = Screen.MAX_NEW_HISTORY + 100
        s = feed(Screen(3, 8), "".join("L%d\r\n" % i for i in range(count)))
        texts = [row_text(line) for line, _ in s.take_new_history()]
        self.assertEqual(len(texts), Screen.MAX_NEW_HISTORY)
        self.assertNotIn("L0", texts, "新しい方を捨てている")
        head = int(texts[0][1:])
        self.assertEqual(texts,
                         ["L%d" % i for i in range(head, head + len(texts))],
                         "差分の途中が抜けている")
        self.assertEqual(s.text()[0], "L%d" % (head + len(texts)),
                         "差分の末尾と画面の先頭が繋がっていない")

    def test_dropping_is_reported_once(self):
        """捨てたことが描画側へ 1 度だけ伝わること。"""
        s = feed(Screen(24, 80), (ESC + "[24S") * 1000)
        self.assertTrue(s.take_history_dropped(), "捨てたのに伝わらない")
        self.assertFalse(s.take_history_dropped(), "忘れていない")

    def test_an_ordinary_screen_reports_nothing_dropped(self):
        """上限に届かない普通の出力では、何も捨てたことにしないこと。"""
        s = feed(Screen(24, 80), "hello\r\n" * 100)
        self.assertFalse(s.take_history_dropped())
        self.assertEqual(len(s.take_new_history()), 77)

    def test_the_history_itself_is_untouched(self):
        """Screen.history の行数はこれまでどおりであること。"""
        s = feed(Screen(24, 80), (ESC + "[24S") * 1000)
        self.assertEqual(len(s.history), 5000)


class HistoryDeltaCapThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def flood(self, rows=24, cols=80, reps=1000):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        terminal._screen = Screen(rows, cols)
        w.queue_output("dev", "marker\r\n")
        w._flush_pending_output()
        w.queue_output("dev", (ESC + "[%dS" % rows) * reps)
        w._flush_pending_output()
        return w, terminal

    def test_the_flood_is_drawn_in_one_pass(self):
        """1 回の描画で描き切れ、描き残しが出ないこと。"""
        w, terminal = self.flood()
        self.assertFalse(w._pending_output, "描き残しが出ている")

    def test_the_document_is_what_it_was_without_the_cap(self):
        """文書は上限行ちょうどで、押し出された古い行だけが残ること。"""
        w, terminal = self.flood()
        limit = w.MAX_DOCUMENT_BLOCKS
        self.assertEqual(terminal.document().blockCount(), limit)
        text = terminal.toPlainText()
        self.assertNotIn("marker", text, "上限を超えた行が残っている")
        self.assertEqual(text.strip(), "", "空行以外が混ざっている")

    def test_the_save_all_log_warning_is_raised(self):
        """捨てた事実が「全ログ保存」の欠落警告に届くこと。"""
        w, terminal = self.flood()
        self.assertTrue(getattr(terminal, "_log_truncated", False))


if __name__ == "__main__":
    unittest.main()
