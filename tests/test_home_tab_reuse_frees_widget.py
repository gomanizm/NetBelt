"""ホームタブを捨てて接続タブにするときも、そのウィジェットを解放することを検証する。

_close_tab は removeTab のあとに親を外して deleteLater するようになったが、
create_terminal_tab にあるもう一方の removeTab（最後のタブがホームタブの
ときに捨てる経路）はそのままだった。QTabWidget.removeTab はページを内部の
QStackedWidget から外さないので、ホームタブ用の QTextEdit が非表示のまま
子として残り続ける。実測: 接続 → 切断（タブを閉じる）を 5 回繰り返すと、
タブは 1 枚なのに tab_widget の QTextEdit の子が 6 個になった。

ホームタブも親から外して deleteLater で捨てる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class HomeTabReuseFreesWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _flush_deferred_deletes():
        from PyQt6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        return w

    def test_home_tab_is_deleted_when_it_is_replaced_by_a_connection(self):
        """捨てられたホームタブの C++ 側が破棄されること。"""
        from PyQt6 import sip
        w = self._widget()
        home = w.tab_widget.widget(0)
        self.assertEqual(w.tab_widget.tabText(0), "ホーム", "前提: ホームタブがある")

        w.create_terminal_tab("dev")
        self._flush_deferred_deletes()

        self.assertTrue(sip.isdeleted(home),
                        "捨てたホームタブのウィジェットが残っている")

    def test_repeated_connect_and_close_leaves_no_hidden_text_edits(self):
        """接続と切断を繰り返しても、隠れた QTextEdit が積み上がらないこと。"""
        from PyQt6.QtWidgets import QTextEdit
        w = self._widget()

        for _ in range(5):
            w.create_terminal_tab("dev")
            index = w.tab_widget.indexOf(w._terminals["dev"])
            w._close_tab(index)
            self._flush_deferred_deletes()

        self.assertEqual(w.tab_widget.count(), 1, "前提: ホームタブ 1 枚に戻っている")
        leftovers = w.tab_widget.findChildren(QTextEdit)
        self.assertEqual(len(leftovers), 1,
                         "表示中のホームタブ以外の QTextEdit が %d 個残っている"
                         % (len(leftovers) - 1))


if __name__ == "__main__":
    unittest.main()
