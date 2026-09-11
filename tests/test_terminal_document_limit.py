"""表示文書にも上限があり、Undo 履歴を溜めないことを検証する。

Screen.history の 5000 行上限は、QTextDocument へ写した後の行には効かない。
blockCount は受信行数のまま増え続け（実測: 50k 行で約 +75MB）、さらに
Undo/Redo が既定の有効なので、スクロールバックを増やさない画面内の
上書き更新でも undo スタックが積み上がる（実測: 20 万回で +70MB、
Undo 無効の対照では +0.9MB）。長時間の接続でプロセスメモリが単調増加する。
Undo を利用者が発動する経路も無い（Ctrl+Z は機器へ送る）。

文書のブロック数に上限を置き、Undo 履歴は持たない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class TerminalDocumentLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        return w, terminal

    def _feed_lines(self, w, count, start=0):
        # 1000 行ずつまとめて流す（1 行ずつだと描画回数が多く遅い）
        for base in range(start, start + count, 1000):
            n = min(1000, start + count - base)
            w.append_output("dev", "".join(
                "line %06d\r\n" % i for i in range(base, base + n)))

    def test_undo_history_is_disabled_on_every_terminal(self):
        """接続タブでもホームタブでも Undo 履歴を持たないこと。"""
        w, terminal = self._widget()
        self.assertFalse(terminal.isUndoRedoEnabled())
        self.assertFalse(terminal.document().isUndoRedoEnabled())
        home = w._create_terminal()
        self.assertFalse(home.isUndoRedoEnabled())

    def test_overwriting_the_screen_leaves_no_undo_steps(self):
        """画面内の上書き更新を繰り返しても undo が積まれないこと。"""
        w, terminal = self._widget()
        for i in range(300):
            w.append_output("dev", "\rcount %d" % i)
        self.assertEqual(terminal.document().availableUndoSteps(), 0)
        self.assertFalse(terminal.document().isUndoAvailable())

    def test_document_block_count_is_capped(self):
        """受信行数が上限を超えても blockCount が上限で頭打ちになること。"""
        from ui.terminal_widget import TerminalWidget
        limit = TerminalWidget.MAX_DOCUMENT_BLOCKS
        w, terminal = self._widget()
        self._feed_lines(w, limit + 3000)
        self.assertLessEqual(terminal.document().blockCount(), limit,
                             "文書の行数に上限が効いていない")

    def test_the_screen_is_still_rendered_correctly_past_the_cap(self):
        """上限で先頭が削られても、画面領域の描画がずれないこと。"""
        from ui.terminal_widget import TerminalWidget
        limit = TerminalWidget.MAX_DOCUMENT_BLOCKS
        w, terminal = self._widget()
        self._feed_lines(w, limit + 3000)
        w.append_output("dev", "Router#show clock\r\n12:00:00 UTC\r\nRouter#")

        lines = terminal.toPlainText().split("\n")
        self.assertEqual(lines[-1].rstrip(), "Router#")
        tail = [ln for ln in lines if ln.strip()]
        self.assertEqual(tail[-3:], ["Router#show clock", "12:00:00 UTC",
                                     "Router#"],
                         "上限到達後の画面が正しく描かれていない: %r" % tail[-5:])
        last_numbered = "line %06d" % (limit + 3000 - 1)
        self.assertIn(last_numbered, tail[-4])
        self.assertNotIn("line 000000", terminal.toPlainText(),
                         "先頭の行が削られていない")


if __name__ == "__main__":
    unittest.main()
