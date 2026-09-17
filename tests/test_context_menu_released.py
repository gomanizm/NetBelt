"""端末を右クリックしても、メニューが端末の子として積み上がらないことを検証する。

contextMenuEvent() は QMenu と QAction を端末（InteractiveTerminal）を親に
して作り、exec() から戻ったあと捨てていなかった。右クリック 1 回につき
QMenu 1 件と QAction 5 件が子として残り、開くたびに単調に増える
（実測: 5 回で {'QMenu': 5, 'QAction': 25}）。

右クリックは Tera Term と同じく貼り付けになり、メニューそのものを作らなく
なった（利用者判断 2026-09-17。キープアライブとマクロは接続先リストの
機器メニュー「ツール」へ移した）。何度右クリックしても、端末にメニューも
項目も残らないこと。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class ContextMenuReleasedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self):
        """遅延削除まで含めて、溜まったイベントを捌く。"""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtWidgets import QApplication
        for _ in range(3):
            QApplication.processEvents()
            QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("rtrA")
        return w, terminal

    def _right_click(self, terminal):
        """右クリックする（メニューやダイアログが出てもその場で閉じる）。"""
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QContextMenuEvent
        with mock.patch("PyQt6.QtWidgets.QMenu.exec", return_value=None), \
                mock.patch("PyQt6.QtWidgets.QDialog.exec", return_value=0):
            terminal.contextMenuEvent(QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, QPoint(1, 1)))
        self._pump()

    @staticmethod
    def _counts(terminal):
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import QMenu
        return (len(terminal.findChildren(QMenu)),
                len(terminal.findChildren(QAction)))

    def test_right_clicks_leave_no_menus_on_the_terminal(self):
        from PyQt6.QtWidgets import QApplication
        w, terminal = self._terminal()
        self.assertEqual(self._counts(terminal), (0, 0),
                         "前提: まだメニューは無い")
        QApplication.clipboard().setText("show clock")

        for _ in range(5):
            self._right_click(terminal)

        menus, actions = self._counts(terminal)
        self.assertEqual((menus, actions), (0, 0),
                         "右クリックの後に残っている: QMenu %d 件 / QAction %d 件"
                         % (menus, actions))

    def test_nothing_piles_up_with_macros_and_several_lines_either(self):
        """マクロ一覧があり、確認ダイアログが出る複数行でも残らないこと。"""
        from PyQt6.QtWidgets import QApplication, QDialog
        w, terminal = self._terminal()
        terminal.set_input_enabled(True)
        terminal.set_macro_list([{"name": "show-run", "description": "設定確認"},
                                 {"name": "show-ver", "description": ""}])
        QApplication.clipboard().setText("show clock\nshow version\n")

        for _ in range(5):
            self._right_click(terminal)

        menus, actions = self._counts(terminal)
        self.assertEqual((menus, actions), (0, 0),
                         "右クリックの後に残っている: QMenu %d 件 / QAction %d 件"
                         % (menus, actions))
        self.assertEqual(len(terminal.findChildren(QDialog)), 0,
                         "閉じた確認ダイアログが残っている")


if __name__ == "__main__":
    unittest.main()
