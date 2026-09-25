"""同じ受信片の中で色を変えてから押し出した行が、履歴でも新しい色になることを検証する。

押し出された行が、文書に「画面の先頭行」としてすでに書いてある文字列と同じ
なら、書き直さずに記録との境目を進めるだけにしている（範囲選択を消さない
ための近道）。比べていたのは文字列だけだったので、同じ文字を別の色で上書き
して、同じ受信片の中でスクロールすると、履歴には古い書式のまま残った。

実測（検証役）: 2 行と 24 行の画面で 'TOP' を通常色で描いたあと、1 回の
append_output で '\\x1b[1;1H\\x1b[31mTOP\\x1b[0m\\x1b[{rows};1H\\r\\n' を渡すと、
画面モデルの押し出し行は赤（fg=1）なのに、文書の先頭の 'TOP' は赤（#cd0000）
ではなかった。色の変更とスクロールを別々の append_output に分けると赤になる。

近道で境目を進めるときに、その行の書式を塗り直すようにした（書式だけなので
文字の位置も範囲選択も変わらない）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

RED = "#cd0000"


class ScrolledLineKeepsNewColourTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self, rows):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        patcher = mock.patch.object(w, "_grid_size", return_value=(rows, 80))
        patcher.start()
        self.addCleanup(patcher.stop)
        terminal = w.create_terminal_tab("dev")
        body = ("TOP\r\n" + "".join("mid%02d\r\n" % i for i in range(rows - 2))
                + "BOTTOM")
        w.append_output("dev", body)
        return w, terminal

    @staticmethod
    def _first_line(terminal):
        """文書の先頭行の文字列と、その 1 文字目の前景色（無ければ None）。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QTextCursor
        block = terminal.document().firstBlock()
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.Right)
        brush = cursor.charFormat().foreground()
        colour = (None if brush.style() == Qt.BrushStyle.NoBrush
                  else brush.color().name())
        return block.text(), colour

    def test_a_recoloured_line_scrolled_off_in_the_same_chunk_keeps_its_colour(self):
        """色を変えた行を同じ受信片で押し出しても、履歴で新しい色になること。"""
        for rows in (2, 24):
            with self.subTest(rows=rows):
                w, terminal = self._terminal(rows)
                self.assertEqual(self._first_line(terminal), ("TOP", None),
                                 "前提: 先頭の TOP は通常色")

                w.append_output("dev", "\x1b[1;1H\x1b[31mTOP\x1b[0m"
                                       "\x1b[%d;1H\r\n" % rows)

                self.assertEqual(terminal._screen.history[-1][0][1].fg, 1,
                                 "前提: 画面モデルでは押し出した TOP は赤")
                self.assertEqual(self._first_line(terminal), ("TOP", RED),
                                 "履歴の TOP が古い色のまま残った")

    def test_a_line_returned_to_the_default_colour_loses_the_old_colour(self):
        """赤だった行を通常色へ戻して同じ受信片で押し出すと、履歴でも通常色になること。"""
        w, terminal = self._terminal(24)
        w.append_output("dev", "\x1b[1;1H\x1b[31mTOP\x1b[0m\x1b[24;7H")
        self.assertEqual(self._first_line(terminal), ("TOP", RED),
                         "前提: 先頭の TOP を赤で描いた")

        w.append_output("dev", "\x1b[1;1HTOP\x1b[24;1H\r\n")

        self.assertEqual(self._first_line(terminal), ("TOP", None),
                         "通常色へ戻したのに、履歴の TOP が赤のまま残った")

    def test_an_unchanged_scrolled_line_keeps_a_selection(self):
        """近道で押し出した行の上の範囲選択は、これまでどおり消えないこと。"""
        from PyQt6.QtGui import QTextCursor
        w, terminal = self._terminal(24)
        cursor = QTextCursor(terminal.document().firstBlock())
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                            QTextCursor.MoveMode.KeepAnchor)
        terminal.setTextCursor(cursor)

        w.append_output("dev", "\r\nnext line\r\n")

        self.assertEqual(terminal.textCursor().selectedText(), "TOP",
                         "押し出しで範囲選択が消えた")


if __name__ == "__main__":
    unittest.main()
