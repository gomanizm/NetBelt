"""描画の途中で例外が出ても、文書の行数上限が守られることを検証する。

画面の書き込み（押し出し行・画面領域の差し替え・塗り直し）を 1 つの編集に
まとめ、先頭の切り捨てを描き終えた後の 1 回にしたため、書き込みの途中で
例外が出ると切り捨てまで届かなかった。src/main.py の excepthook は例外の
あともプロセスを生かし続けるので、以後の受信で文書が上限を超えて伸び続けた
（検査役の指摘: 上限 500 で 1 回 200 行ずつ流すと、例外のたびに 200 行ずつ増えた）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

LIMIT = 300


class RenderFailureKeepsLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        patcher = mock.patch.object(TerminalWidget, "MAX_DOCUMENT_BLOCKS", LIMIT)
        patcher.start()
        self.addCleanup(patcher.stop)
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.resize(900, 500)
        w.show()
        terminal = w.create_terminal_tab("dev")
        self.app.processEvents()
        return w, terminal

    @staticmethod
    def _lines(start, count):
        return "".join("line %06d\r\n" % i for i in range(start, start + count))

    def _failing_paint(self, w):
        # 塗り直しは毎回少なくとも 1 行（カーソルの行）走るので、必ず失敗する
        return mock.patch.object(w, "_paint_row",
                                 side_effect=RuntimeError("injected paint failure"))

    def test_limit_holds_while_painting_keeps_failing(self):
        """塗り直しが毎回失敗しても、各回の後で行数が上限を超えないこと。"""
        w, terminal = self._widget()
        w.append_output("dev", self._lines(0, 2 * LIMIT))
        self.assertLessEqual(terminal.document().blockCount(), LIMIT)

        with self._failing_paint(w):
            for step in range(8):
                with self.assertRaises(RuntimeError):
                    w.append_output("dev", self._lines(1000 + 100 * step, 100))
                self.assertLessEqual(
                    terminal.document().blockCount(), LIMIT,
                    "%d 回目の失敗の後で上限を超えた" % (step + 1))

    def test_lines_dropped_by_a_failed_render_are_marked(self):
        """失敗した描画で先頭の行を捨てたら、全ログ保存の欠落の印が立つこと。"""
        w, terminal = self._widget()
        w.append_output("dev", self._lines(0, LIMIT // 2))
        self.assertFalse(terminal._log_truncated, "前提: まだ何も捨てていない")

        with self._failing_paint(w):
            with self.assertRaises(RuntimeError):
                w.append_output("dev", self._lines(1000, LIMIT))

        self.assertLessEqual(terminal.document().blockCount(), LIMIT)
        self.assertTrue(terminal._log_truncated,
                        "先頭の行を捨てたのに、欠落の印が立っていない")

    def test_drawing_recovers_after_a_failure(self):
        """失敗のあとも、次の描画で最後の行まで出て最下部に追従し、上限も守ること。"""
        w, terminal = self._widget()
        w.append_output("dev", self._lines(0, 2 * LIMIT))
        with self._failing_paint(w):
            with self.assertRaises(RuntimeError):
                w.append_output("dev", self._lines(1000, 100))

        w.append_output("dev", self._lines(2000, 50))
        self.app.processEvents()

        self.assertIn("line 002049", terminal.toPlainText())
        self.assertLessEqual(terminal.document().blockCount(), LIMIT)
        bar = terminal.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum(), "失敗のあと最下部へ追従しない")


if __name__ == "__main__":
    unittest.main()
