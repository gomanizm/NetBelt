"""閉じたタブのターミナルが、文書ごと解放されることを検証する。

QTabWidget.removeTab はページを親（内部の QStackedWidget）から外さない。
閉じたタブの InteractiveTerminal は非表示のまま子として残り、文書
（受信した全出力）も一緒に生き続ける。実測では 40,000 行のタブを閉じる
たびに約 140MB が積み上がり、閉じても戻らなかった。

閉じたタブは親から外して deleteLater で捨てる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class ClosingATabFreesItsTerminalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget_with_tab(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        w.append_output("rtrA", "line one\r\nline two\r\n")
        for i in range(w.tab_widget.count()):
            if w.tab_widget.tabText(i) == "rtrA":
                return w, w._terminals["rtrA"], i
        raise AssertionError("タブが無い")

    @staticmethod
    def _flush_deferred_deletes():
        from PyQt6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_closed_tab_is_no_longer_a_child_of_the_tab_widget(self):
        """閉じたタブが QStackedWidget の子として残らないこと。"""
        from ui.terminal_widget import InteractiveTerminal
        w, terminal, index = self._widget_with_tab()

        w._close_tab(index)

        leftovers = w.tab_widget.findChildren(InteractiveTerminal)
        self.assertEqual(leftovers, [],
                         "閉じたタブのターミナルが親に残っている")

    def test_closed_tab_is_deleted_once_events_are_processed(self):
        """閉じたタブの C++ 側が deleteLater で破棄されること。"""
        from PyQt6 import sip
        w, terminal, index = self._widget_with_tab()

        w._close_tab(index)
        self._flush_deferred_deletes()

        self.assertTrue(sip.isdeleted(terminal),
                        "閉じたタブのターミナルが破棄されていない")

    def test_closing_leaves_the_remaining_tabs_alone(self):
        """他のタブは閉じた影響を受けないこと。"""
        from PyQt6 import sip
        w, terminal, index = self._widget_with_tab()
        other = w.create_terminal_tab("rtrB")
        for i in range(w.tab_widget.count()):
            if w.tab_widget.tabText(i) == "rtrA":
                index = i

        w._close_tab(index)
        self._flush_deferred_deletes()

        self.assertFalse(sip.isdeleted(other))
        self.assertIs(w._terminals["rtrB"], other)
        self.assertEqual(w.tab_widget.count(), 1)


if __name__ == "__main__":
    unittest.main()
