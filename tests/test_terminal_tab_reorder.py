"""ターミナルのタブを並べ替えられること。

機器との対応付けはタブ名とウィジェットで持っており、タブの位置には
依存しない。並べ替えたあとでも、閉じる・切り替える・出力を流すが
正しい機器に当たることを確かめる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class TabReorderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def widget_with_tabs(self, *names):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        for name in names:
            w.create_terminal_tab(name)
        return w

    def tab_order(self, w):
        return [w.tab_widget.tabText(i) for i in range(w.tab_widget.count())]

    def test_the_tab_bar_allows_dragging(self):
        w = self.widget_with_tabs("a")
        self.assertTrue(w.tab_widget.isMovable())

    def test_reordering_moves_the_tab(self):
        w = self.widget_with_tabs("rtr1", "rtr2", "rtr3")
        w.tab_widget.tabBar().moveTab(2, 0)
        self.assertEqual(self.tab_order(w), ["rtr3", "rtr1", "rtr2"])

    def test_output_still_reaches_the_right_terminal(self):
        w = self.widget_with_tabs("rtr1", "rtr2", "rtr3")
        w.tab_widget.tabBar().moveTab(2, 0)
        w.append_output("rtr2", "hostname rtr2")
        self.assertIn("hostname rtr2", w._terminals["rtr2"].toPlainText())
        self.assertNotIn("hostname rtr2", w._terminals["rtr3"].toPlainText())

    def test_closing_after_a_reorder_closes_the_one_clicked(self):
        w = self.widget_with_tabs("rtr1", "rtr2", "rtr3")
        w.tab_widget.tabBar().moveTab(2, 0)      # rtr3, rtr1, rtr2
        closed = []
        w.tab_closed.connect(closed.append)
        w._close_tab(0)                          # 先頭 = rtr3
        self.assertEqual(closed, ["rtr3"])
        self.assertEqual(self.tab_order(w), ["rtr1", "rtr2"])
        self.assertNotIn("rtr3", w._terminals)

    def test_the_current_tab_name_follows_the_new_order(self):
        w = self.widget_with_tabs("rtr1", "rtr2", "rtr3")
        w.tab_widget.tabBar().moveTab(2, 0)
        w.tab_widget.setCurrentIndex(0)
        self.assertEqual(w.get_current_tab_name(), "rtr3")

    def test_reusing_a_tab_finds_it_wherever_it_sits(self):
        """再接続はタブ名で探すので、位置が変わっても同じタブを使う。"""
        w = self.widget_with_tabs("rtr1", "rtr2", "rtr3")
        w.tab_widget.tabBar().moveTab(0, 2)      # rtr2, rtr3, rtr1
        before = w._terminals["rtr1"]
        again = w.create_terminal_tab("rtr1")
        self.assertIs(again, before)
        self.assertEqual(w.tab_widget.count(), 3)


if __name__ == "__main__":
    unittest.main()
