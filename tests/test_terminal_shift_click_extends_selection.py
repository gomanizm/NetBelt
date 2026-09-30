"""端末で Shift+クリックすると、いまの選択範囲がそこまで広がることを検証する。

何が起きていたか（441ea02 で実測。scratchpad\\v132-triage\\termui\\
repro_ui_shift_click.py）: '12:00:00' をドラッグで選び（クリップボードも
'12:00:00'）、同じ行の右を Shift+クリックすると、選択が '' に消えた
（クリップボードは元のまま）。素の QTextEdit で同じ操作をすると
'12:00:00.000 UTC We' へ広がる。1.3.1 で入った InteractiveTerminal.
mousePressEvent が、左ボタンで選択があると Shift の有無を見ずに選択を
外してから Qt へ渡していた。Qt の Shift+クリックは選択の起点（anchor）
から広げるので、起点がクリック位置へ移って選択が空になっていた（1.3.1 の後退）。

どう直したか: 選択を外すのは、修飾キーが Shift だけではないときに限る。
Qt は修飾キーが Shift だけのときに限って選択を広げ、そのときはテキストの
ドラッグを始めない（範囲の上で押しても運ばない）。それ以外（Ctrl+Shift
など）はこれまでどおり選択を外してから渡すので、選択範囲のドラッグで
機器へ送られる件（1.3.1 で直した件）は戻らない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class TerminalShiftClickExtendsSelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText("BEFORE")

    def tearDown(self):
        # QTest に Shift 付きで押させると、Shift を押したままの状態がアプリ
        # 全体に残る。残すと後のテストのクリックが範囲の拡張として扱われる
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        if self._terminal_widget is not None:
            QTest.keyRelease(self._terminal_widget, Qt.Key.Key_Shift)

    _terminal_widget = None

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
        self._terminal_widget = terminal
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

    def _drag(self, terminal, start, end, modifiers=None):
        """左ボタンを押したまま start から end まで動かして離す。"""
        from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        if modifiers is None:
            modifiers = Qt.KeyboardModifier.NoModifier
        viewport = terminal.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, modifiers, start)
        steps = 6
        for i in range(1, steps + 1):
            p = QPoint(start.x() + (end.x() - start.x()) * i // steps,
                       start.y() + (end.y() - start.y()) * i // steps)
            QApplication.sendEvent(viewport, QMouseEvent(
                QEvent.Type.MouseMove, QPointF(p),
                QPointF(viewport.mapToGlobal(p)),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                modifiers))
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, modifiers, end)
        self.app.processEvents()

    def test_shift_click_extends_the_selection(self):
        """ドラッグで選んだあとの Shift+クリックで、選択とクリップボードが広がること。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        w, terminal = self._terminal()
        self._drag(terminal, self._point(terminal, 1, 1),
                   self._point(terminal, 1, 9))
        self.assertEqual(terminal.textCursor().selectedText(), "12:00:00",
                         "前提: '12:00:00' を選んでいる")
        sent = []
        terminal.key_pressed.connect(sent.append)

        QTest.mouseClick(terminal.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ShiftModifier,
                         self._point(terminal, 1, 20))
        self.app.processEvents()

        self.assertEqual(terminal.textCursor().selectedText(),
                         "12:00:00.000 UTC We",
                         "Shift+クリックで選択範囲が広がらない")
        self.assertEqual(QApplication.clipboard().text(),
                         "12:00:00.000 UTC We")
        self.assertEqual(sent, [], "Shift+クリックで機器へ何か送った")

    def test_shift_drag_from_inside_a_selection_does_not_carry_the_text(self):
        """選択範囲の上から Shift を押してドラッグしても、テキストを運ばないこと。

        Shift のときは選択を外さずに Qt へ渡すので、Qt が選択範囲の
        ドラッグ＆ドロップを始めないこと（起点から広げ直しになる）を見る。
        """
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication
        w, terminal = self._terminal()
        self._drag(terminal, self._point(terminal, 0, 0),
                   self._point(terminal, 0, 17))
        self.assertEqual(terminal.textCursor().selectedText(),
                         "Router#show clock", "前提: 1 行目を選んでいる")
        sent = []
        terminal.key_pressed.connect(sent.append)

        self._drag(terminal, self._point(terminal, 0, 7),
                   self._point(terminal, 0, 11),
                   Qt.KeyboardModifier.ShiftModifier)

        self.assertEqual(terminal.textCursor().selectedText(), "Router#show",
                         "Shift+ドラッグが選択の起点からの広げ直しにならない")
        self.assertEqual(QApplication.clipboard().text(), "Router#show")
        self.assertEqual(sent, [], "Shift+ドラッグで機器へ何か送った")


if __name__ == "__main__":
    unittest.main()
