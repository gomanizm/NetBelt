"""範囲選択していた行が上限で捨てられたとき、キャレットが画面カーソルへ戻ることを検証する。

先頭の切り捨てを描き終えた後の 1 回にまとめたとき、キャレットを置く判定の
あとで切っていたので、選択が残っている（動かさない）と判断した直後に選択ごと
行が消え、キャレットが文書の先頭（位置 0）に取り残された。変更前は押し出し行を
書いた直後に切っていたので、選択が消えてからキャレットを画面カーソルへ置けていた。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

LIMIT = 300


class TrimmedSelectionCaretTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_caret_returns_to_the_screen_when_the_selection_is_trimmed(self):
        from PyQt6.QtGui import QTextCursor
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
        w.append_output("dev", "".join("old %05d\r\n" % i for i in range(250)))

        doc = terminal.document()
        selection = QTextCursor(doc)
        selection.setPosition(doc.findBlockByNumber(10).position())
        selection.setPosition(doc.findBlockByNumber(20).position() + 3,
                              QTextCursor.MoveMode.KeepAnchor)
        terminal.setTextCursor(selection)
        self.assertTrue(terminal.textCursor().hasSelection(), "前提: 範囲選択している")

        # 選択している行（10〜20 行目）が上限で捨てられる量を流す
        w.append_output("dev", "".join("new %05d\r\n" % i for i in range(200)))
        self.app.processEvents()

        self.assertNotIn("old 00015", terminal.toPlainText(), "前提: 選択していた行が捨てられた")
        caret = terminal.textCursor()
        self.assertFalse(caret.hasSelection())
        self.assertGreaterEqual(
            caret.position(), terminal._region.position(),
            "キャレットが画面カーソルではなく位置 %d に残った" % caret.position())
        self.assertEqual(caret.block().text(), "",
                         "キャレットがカーソルの行（最後の出力の次の空行）にない")


if __name__ == "__main__":
    unittest.main()
