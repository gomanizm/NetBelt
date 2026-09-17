"""出力が続いている最中でも、上へスクロールして過去の出力を読めることを検証する。

_render_screen は描画のたびに無条件でスクロールバーを最下部へ動かしていた。
terminal length 0 のあとの show running-config all のように出力が続くと、
上へスクロールしても 1 行届くたびに最下部へ戻され、流れ終わるまで読めない
（実測: 300 行流して一番上へスクロール → 1 行届くだけで最下部へ戻る。
範囲選択中も戻る）。キャレット移動（setTextCursor）も見えるところまで
スクロールするので、そちらだけでも下へ寄る。選択せずにクリックしたときに
キャレットを末尾へ戻す mouseReleaseEvent も同じ理由で下へ飛ぶ。

最下部を見ているときだけ出力に追従し、上へスクロールしているときは
同じ行を見せ続ける。古い行が上限で捨てられても、見ている行がずれないこと。
打鍵したら最下部へ戻る（打った文字のエコーが見えるように）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class TerminalScrollPositionKeptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, cap=None):
        from ui.terminal_widget import TerminalWidget
        if cap is not None:
            patcher = mock.patch.object(TerminalWidget, "MAX_DOCUMENT_BLOCKS", cap)
            patcher.start()
            self.addCleanup(patcher.stop)
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.resize(900, 500)
        w.show()
        terminal = w.create_terminal_tab("dev")
        self.app.processEvents()
        return w, terminal

    def _feed(self, w, start, count):
        w.append_output("dev", "".join(
            "line %06d\r\n" % i for i in range(start, start + count)))
        self.app.processEvents()

    @staticmethod
    def _top_line(terminal):
        """表示領域のいちばん上に見えている行の文字列。"""
        from PyQt6.QtCore import QPoint
        return terminal.cursorForPosition(QPoint(0, 0)).block().text()

    def test_new_output_does_not_pull_a_scrolled_up_view_to_the_bottom(self):
        """上へスクロールしている間は、出力が届いても位置が動かないこと。"""
        w, terminal = self._widget()
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        bar.setValue(0)
        top = self._top_line(terminal)

        self._feed(w, 300, 1)

        self.assertEqual(bar.value(), 0, "出力で最下部へ戻された")
        self.assertEqual(self._top_line(terminal), top)

    def test_a_selection_does_not_change_that(self):
        """範囲選択していても、出力で最下部へ戻されないこと。"""
        from PyQt6.QtGui import QTextCursor
        w, terminal = self._widget()
        self._feed(w, 0, 300)
        cursor = QTextCursor(terminal.document())
        cursor.setPosition(0)
        cursor.setPosition(20, QTextCursor.MoveMode.KeepAnchor)
        terminal.setTextCursor(cursor)
        bar = terminal.verticalScrollBar()
        bar.setValue(0)

        self._feed(w, 300, 5)

        self.assertEqual(bar.value(), 0, "範囲選択中に最下部へ戻された")
        self.assertTrue(terminal.textCursor().hasSelection())

    def test_a_view_at_the_bottom_follows_new_output(self):
        """最下部を見ているときは、これまでどおり出力に追従すること。"""
        w, terminal = self._widget()
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum(), "前提: 最下部を見ている")
        before = bar.maximum()

        self._feed(w, 300, 50)

        self.assertGreater(bar.maximum(), before, "前提: 文書が伸びた")
        self.assertEqual(bar.value(), bar.maximum(), "新しい出力に追従していない")

    def test_the_same_lines_stay_in_view_while_old_lines_are_dropped(self):
        """上限で先頭の行が捨てられても、見ている行がずれないこと。"""
        cap = 400
        w, terminal = self._widget(cap)
        self._feed(w, 0, 600)
        self.assertEqual(terminal.document().blockCount(), cap,
                         "前提: 文書が上限まで詰まっている")
        bar = terminal.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        self.app.processEvents()
        top = self._top_line(terminal)

        self._feed(w, 600, 50)

        self.assertEqual(self._top_line(terminal), top,
                         "古い行が捨てられた拍子に、見ている行がずれた")

    def test_typing_brings_the_view_back_to_the_bottom(self):
        """打鍵したら最下部へ戻ること（打った文字のエコーを見せるため）。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        w, terminal = self._widget()
        terminal.set_input_enabled(True)
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        bar.setValue(0)

        QTest.keyClick(terminal, Qt.Key.Key_A)

        self.assertEqual(bar.value(), bar.maximum(), "打鍵しても最下部へ戻らない")

    def test_pressing_a_modifier_alone_does_not_move_the_view(self):
        """修飾キーだけでは動かないこと（Ctrl+ホイールでの拡大縮小などのため）。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        w, terminal = self._widget()
        terminal.set_input_enabled(True)
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        bar.setValue(0)

        QTest.keyClick(terminal, Qt.Key.Key_Control)

        self.assertEqual(bar.value(), 0, "修飾キーだけで最下部へ戻った")

    def test_clicking_without_selecting_does_not_move_a_scrolled_up_view(self):
        """選択せずにクリックしても、スクロール位置が動かないこと。"""
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest
        w, terminal = self._widget()
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        bar.setValue(0)

        QTest.mouseClick(terminal.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(5, 5))

        self.assertEqual(bar.value(), 0, "クリックで最下部へ飛んだ")


if __name__ == "__main__":
    unittest.main()
