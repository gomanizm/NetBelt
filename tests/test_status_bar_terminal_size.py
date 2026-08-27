"""ステータスバーに端末の大きさを出す。

不具合の切り分けで「そのとき窓が何桁だったか」が分からず往復した
ので、いま機器へ伝えている大きさをその場で見えるようにした。

一時メッセージ (showMessage) とは場所を取り合わないこと、他の表示を
壊さないことを確かめる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class TerminalSizeStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def window(self):
        from ui.main_window import MainWindow
        return MainWindow()

    def test_the_label_lives_in_the_permanent_area(self):
        """常設欄に置くこと。showMessage と場所を取り合わない。"""
        w = self.window()
        w.status_bar.showMessage("接続しました")
        w._show_terminal_size("")
        self.assertEqual(w.status_bar.currentMessage(), "接続しました")
        self.assertIsNotNone(w.terminal_size_label.parent())

    def test_nothing_is_shown_without_a_terminal(self):
        w = self.window()
        w._show_terminal_size("")
        self.assertEqual(w.terminal_size_label.text(), "")

    def test_the_size_appears_for_an_open_terminal(self):
        w = self.window()
        w.terminal_widget.create_terminal_tab("dev")
        w._show_terminal_size("dev")
        cols, rows = w.terminal_widget.grid_size_for("dev")
        self.assertEqual(w.terminal_size_label.text(),
                         "%d x %d" % (cols, rows))

    def test_a_resize_updates_the_shown_size(self):
        w = self.window()
        w.terminal_widget.create_terminal_tab("dev")
        w._on_terminal_resized("dev", 132, 43)
        self.assertEqual(w.terminal_size_label.text(), "132 x 43")

    def test_a_resize_of_a_hidden_tab_does_not_overwrite_it(self):
        """見えていないタブの大きさで、表示中のものを上書きしないこと。"""
        w = self.window()
        w.terminal_widget.create_terminal_tab("shown")
        w._on_terminal_resized("shown", 100, 30)
        w._on_terminal_resized("other", 40, 10)
        self.assertEqual(w.terminal_size_label.text(), "100 x 30")


if __name__ == "__main__":
    unittest.main()
