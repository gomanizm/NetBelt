"""幅 0 まで畳んだツールエリアを、メニューから戻せること。

仕切りを右端まで引いた状態は、見た目は消えているのにウィジェットとしては
表示中（isHidden() は False）。そのため「ツールエリア表示/非表示」は
まず隠す側へ倒れ、2 回押しても幅 0 のまま戻らなかった。表示メニューの
各ツール（SFTPクライアント等）を選んでも同じで、タブの選択だけが変わる。
しかも幅 0 は ui_layout.splitter_sizes に保存され次回起動へ持ち越される。

接続先リスト側には同じ復元処理が既にある。ツールエリアにも入れる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class ToolAreaHidingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.windows = []       # ウィンドウを先に捨てると子ごと消えるので保持する

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-tool-area-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self).windows.append(w)
        return w

    def _collapse(self, w):
        """仕切りを右端まで引いた状態（幅 0・isHidden は False）にする。"""
        sizes = w.main_splitter.sizes()
        w.main_splitter.setSizes([sizes[0], sizes[1] + sizes[2], 0])
        self.assertFalse(w.tool_tabs.isHidden())
        self.assertEqual(w.main_splitter.sizes()[2], 0)

    def test_one_click_brings_back_an_area_collapsed_to_zero(self):
        w = self._window()
        self._collapse(w)

        w._toggle_tool_area()          # 1 回で戻る

        self.assertFalse(w.tool_tabs.isHidden())
        self.assertGreaterEqual(w.main_splitter.sizes()[2], 100)
        self.assertTrue(w.toggle_tool_area_action.isChecked())

    def test_choosing_a_tool_from_the_view_menu_brings_it_back(self):
        """表示メニューでツールを選んだときも、幅を取り戻すこと。"""
        w = self._window()
        self._collapse(w)

        w._select_tool_tab("sftp")

        self.assertGreaterEqual(w.main_splitter.sizes()[2], 100)
        self.assertEqual(w.tool_tabs.currentIndex(), w._tab_index["sftp"])

    def test_coming_back_from_hiding_has_width(self):
        """隠してから戻したときも、幅があること。"""
        w = self._window()
        self._collapse(w)
        w.tool_tabs.setVisible(False)

        w._toggle_tool_area()

        self.assertFalse(w.tool_tabs.isHidden())
        self.assertGreaterEqual(w.main_splitter.sizes()[2], 100)

    def test_hiding_still_works(self):
        """幅がある状態では、これまでどおり隠す側へ倒れること。"""
        w = self._window()
        sizes = w.main_splitter.sizes()
        w.main_splitter.setSizes([sizes[0], 400, 300])

        w._toggle_tool_area()

        self.assertTrue(w.tool_tabs.isHidden())
        self.assertFalse(w.toggle_tool_area_action.isChecked())

    def test_the_device_list_is_not_disturbed(self):
        """接続先リストの幅を巻き込まないこと。"""
        w = self._window()
        w.main_splitter.setSizes([250, 700, 0])
        before = w.main_splitter.sizes()[0]

        w._toggle_tool_area()

        self.assertEqual(w.main_splitter.sizes()[0], before)
        self.assertFalse(w.device_tree.isHidden())


if __name__ == "__main__":
    unittest.main()
