"""端末の範囲選択が Tera Term と同じく、選んだ時点でクリップボードへ入ることを検証する。

ドラッグで範囲選択はできたが、クリップボードは変わらなかった（実測）。
コピーは右クリックメニューか 編集→コピー（Ctrl+Shift+C）だけだった。

また端末は編集可能な QTextEdit なので、選択範囲の上から押してドラッグすると
テキストのドラッグ＆ドロップが始まり、落とした先で機器へ送られていた
（利用者報告。改行は CR になるので各行が実行される）。選択範囲の上から
ドラッグしても、新しい範囲選択になること。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class TerminalSelectionCopyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText("BEFORE")

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.resize(900, 500)
        w.show()
        terminal = w.create_terminal_tab("dev")
        terminal.set_input_enabled(True)
        w.append_output("dev", "Router#show clock\r\n"
                               "*12:00:00.000 UTC Wed Sep 17 2026\r\nRouter#")
        self.app.processEvents()
        return w, terminal

    @staticmethod
    def _point(terminal, row, col):
        """row 行目 col 文字目の左端の少し右（ビューポート座標）。"""
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QTextCursor
        cursor = QTextCursor(terminal.document().findBlockByNumber(row))
        cursor.setPosition(cursor.position() + col)
        rect = terminal.cursorRect(cursor)
        return QPoint(rect.left() + 2, rect.center().y())

    def _drag(self, terminal, start, end):
        """左ボタンを押したまま start から end まで動かして離す。"""
        from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        viewport = terminal.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, start)
        steps = 6
        for i in range(1, steps + 1):
            p = QPoint(start.x() + (end.x() - start.x()) * i // steps,
                       start.y() + (end.y() - start.y()) * i // steps)
            QApplication.sendEvent(viewport, QMouseEvent(
                QEvent.Type.MouseMove, QPointF(p),
                QPointF(viewport.mapToGlobal(p)),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, end)
        self.app.processEvents()

    def test_dragging_a_selection_copies_it_to_the_clipboard(self):
        """ドラッグで選んだ範囲が、離した時点でクリップボードに入ること。"""
        from PyQt6.QtWidgets import QApplication
        w, terminal = self._terminal()

        self._drag(terminal, self._point(terminal, 0, 0),
                   self._point(terminal, 0, 11))

        self.assertEqual(terminal.textCursor().selectedText(), "Router#show")
        self.assertEqual(QApplication.clipboard().text(), "Router#show",
                         "選んだ範囲がクリップボードに入っていない")

    def test_a_click_without_dragging_leaves_the_clipboard_alone(self):
        """選択せずにクリックしただけなら、クリップボードを変えないこと。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        w, terminal = self._terminal()

        QTest.mouseClick(terminal.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         self._point(terminal, 1, 3))
        self.app.processEvents()

        self.assertEqual(QApplication.clipboard().text(), "BEFORE")

    def test_dragging_from_inside_a_selection_selects_again(self):
        """選択範囲の上からドラッグしたら、テキストを運ばず新しい範囲を選ぶこと。"""
        from PyQt6.QtWidgets import QApplication
        w, terminal = self._terminal()
        self._drag(terminal, self._point(terminal, 0, 0),
                   self._point(terminal, 0, 17))
        self.assertEqual(terminal.textCursor().selectedText(),
                         "Router#show clock", "前提: 1 行目を選んでいる")
        sent = []
        terminal.key_pressed.connect(sent.append)

        self._drag(terminal, self._point(terminal, 0, 7),
                   self._point(terminal, 0, 11))

        self.assertEqual(terminal.textCursor().selectedText(), "show",
                         "選択範囲の上からのドラッグが新しい選択にならない")
        self.assertEqual(QApplication.clipboard().text(), "show")
        self.assertEqual(sent, [], "ドラッグで機器へ何か送った")


if __name__ == "__main__":
    unittest.main()
