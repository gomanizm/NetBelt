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
import re
import sys
import unittest
from unittest import mock

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


class CappedDocumentSurvivesResizeTest(unittest.TestCase):
    """上限に達した文書を縦にリサイズしても、記録が壊れないことを検証する。

    _render_screen は画面領域の先頭を int (start) で控えてから文書を書き
    換える。上限に達していると、その書き換えで Qt が文書の先頭ブロックを
    捨てるため、QTextCursor である region は自動で詰まるのに start だけが
    古い位置を指したままになる。ずれた start を次の差し替え範囲・塗り直し
    位置・キャレット位置に使うので、縦にリサイズするたびにスクロール
    バックへ重複行と欠落が積み上がり、それが画面にも「全ログ保存」にも
    そのまま出る。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, cap):
        """文書の上限を cap 行にした端末を返す。"""
        from ui.terminal_widget import TerminalWidget
        patcher = mock.patch.object(TerminalWidget, "MAX_DOCUMENT_BLOCKS", cap)
        patcher.start()
        self.addCleanup(patcher.stop)
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        return w, terminal

    @staticmethod
    def _numbers(terminal):
        """文書に残っている 'line NNNNNN' の連番を出た順に返す。"""
        return [int(m) for m in re.findall(r"line (\d{6})",
                                           terminal.toPlainText())]

    def _resize_cycles(self, w, terminal, cycles):
        """画面の行数を 1 行ぶん往復させる（_apply_grid_size と同じ手順）。"""
        screen = terminal._screen
        base = screen.rows
        for i in range(cycles):
            screen.set_size(base - 1 if i % 2 == 0 else base, screen.cols)
            w._render_screen(terminal)

    def test_resizing_a_capped_document_keeps_the_scrollback_intact(self):
        """上限到達後にリサイズしても、連番に重複と断裂が出ないこと。"""
        cap = 400
        w, terminal = self._widget(cap)
        for i in range(cap * 2):
            w.append_output("dev", "line %06d\r\n" % i)
        self.assertEqual(terminal.document().blockCount(), cap,
                         "前提: 文書が上限まで切り詰められている")

        self._resize_cycles(w, terminal, 6)

        numbers = self._numbers(terminal)
        dups = [n for n in set(numbers) if numbers.count(n) > 1]
        self.assertEqual(dups, [],
                         "リサイズで行が重複した: %r" % sorted(dups)[:10])
        gaps = [(a, b) for a, b in zip(numbers, numbers[1:]) if b != a + 1]
        self.assertEqual(gaps, [],
                         "リサイズで行が抜けた: %r" % gaps[:10])

    def test_resizing_below_the_cap_is_unaffected(self):
        """上限に達していなければ、これまでどおり壊れないこと（対照）。"""
        cap = 400
        w, terminal = self._widget(cap)
        for i in range(50):
            w.append_output("dev", "line %06d\r\n" % i)
        self.assertLess(terminal.document().blockCount(), cap,
                        "前提: 文書は上限に達していない")

        self._resize_cycles(w, terminal, 6)

        numbers = self._numbers(terminal)
        self.assertEqual(numbers, sorted(set(numbers)),
                         "上限に達していないのに行が壊れた")


if __name__ == "__main__":
    unittest.main()
