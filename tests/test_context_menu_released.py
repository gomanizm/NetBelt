"""右クリックメニューが、端末の子として積み上がらないことを検証する。

contextMenuEvent() は QMenu と QAction を端末（InteractiveTerminal）を親に
して作り、exec() から戻ったあと捨てていなかった。右クリック 1 回につき
QMenu 1 件と QAction 5 件が子として残り、開くたびに単調に増える
（実測: 5 回で {'QMenu': 5, 'QAction': 25}）。マクロ一覧があるときは
サブメニューとマクロの項目も同じだけ増える。

閉じたメニューは項目ごと破棄されて、繰り返しても増えないこと。
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

    def _open_menu(self, terminal):
        """メニューを開いて、そのまま閉じる（exec は使わない）。"""
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QContextMenuEvent

        opened = {}

        def fake_exec(menu, *args, **kwargs):
            opened["count"] = len(menu.actions())
            return None

        with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
            terminal.contextMenuEvent(QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, QPoint(1, 1)))
        self._pump()
        return opened["count"]

    @staticmethod
    def _counts(terminal):
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import QMenu
        return (len(terminal.findChildren(QMenu)),
                len(terminal.findChildren(QAction)))

    def test_closed_menus_do_not_pile_up_on_the_terminal(self):
        w, terminal = self._terminal()
        self.assertEqual(self._counts(terminal), (0, 0),
                         "前提: まだメニューは無い")

        self.assertGreater(self._open_menu(terminal), 0,
                           "前提: 項目のあるメニューが開いている")
        for _ in range(4):
            self._open_menu(terminal)

        menus, actions = self._counts(terminal)
        self.assertEqual((menus, actions), (0, 0),
                         "閉じたメニューが残っている: QMenu %d 件 / QAction %d 件"
                         % (menus, actions))

    def test_macro_submenu_items_do_not_pile_up_either(self):
        w, terminal = self._terminal()
        terminal.set_input_enabled(True)
        terminal.set_macro_list([{"name": "show-run", "description": "設定確認"},
                                 {"name": "show-ver", "description": ""}])

        for _ in range(5):
            self._open_menu(terminal)

        menus, actions = self._counts(terminal)
        self.assertEqual((menus, actions), (0, 0),
                         "閉じたメニューが残っている: QMenu %d 件 / QAction %d 件"
                         % (menus, actions))


if __name__ == "__main__":
    unittest.main()
