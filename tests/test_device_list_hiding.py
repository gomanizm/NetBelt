"""接続先リストを隠せること。

ツールエリアと同じ 3 つの経路を用意する:
  - 仕切りをドラッグして畳む
  - 右クリックメニューから隠す
  - 表示メニューで出し入れする（隠したあと戻す唯一の道）

隠すのは簡単でも戻せないと困るので、戻したときに幅が 0 のままに
ならないことも見る。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class DeviceListHidingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def window(self):
        from ui.main_window import MainWindow
        return MainWindow()

    def test_the_handle_can_collapse_the_list(self):
        """仕切りをドラッグして畳めること。"""
        w = self.window()
        self.assertTrue(w.main_splitter.isCollapsible(0))

    def test_the_terminal_can_never_be_collapsed(self):
        """ターミナルは本体なので畳ませないこと。"""
        w = self.window()
        self.assertFalse(w.main_splitter.isCollapsible(1))

    def test_the_context_menu_asks_the_window_to_hide_it(self):
        """右クリックの「非表示」がウィンドウへ伝わること。"""
        w = self.window()
        self.assertFalse(w.device_tree.isHidden())
        w.device_tree.hide_requested.emit()
        self.assertTrue(w.device_tree.isHidden())

    def test_the_view_menu_brings_it_back(self):
        w = self.window()
        w._toggle_device_list()
        self.assertTrue(w.device_tree.isHidden())
        self.assertFalse(w.toggle_device_list_action.isChecked())
        w._toggle_device_list()
        self.assertFalse(w.device_tree.isHidden())
        self.assertTrue(w.toggle_device_list_action.isChecked())

    def test_one_click_brings_back_a_list_collapsed_to_zero(self):
        """幅 0 まで畳んだ状態からは、1 回で戻ること。

        分割位置は次回起動へ持ち越される。幅 0 のまま終了したあと、
        表示メニューを押しても何も起きない（実際には一度隠してから
        出し直している）ように見えると、戻せないと受け取られる。
        """
        w = self.window()
        sizes = w.main_splitter.sizes()
        w.main_splitter.setSizes([0, sum(sizes[:2])] + sizes[2:])
        w._toggle_device_list()          # 1 回で戻る
        self.assertFalse(w.device_tree.isHidden())
        self.assertGreaterEqual(w.main_splitter.sizes()[0], 100)

    def test_coming_back_from_hiding_has_width(self):
        """隠してから戻したときも、幅があること。"""
        w = self.window()
        sizes = w.main_splitter.sizes()
        w.main_splitter.setSizes([0, sum(sizes[:2])] + sizes[2:])
        w.device_tree.setVisible(False)
        w._toggle_device_list()
        self.assertFalse(w.device_tree.isHidden())
        self.assertGreaterEqual(w.main_splitter.sizes()[0], 100)

    def test_the_tool_area_still_toggles_on_its_own(self):
        """ツールエリアの表示切替を巻き込んでいないこと。"""
        w = self.window()
        w._toggle_device_list()
        self.assertFalse(w.tool_tabs.isHidden())
        w._toggle_tool_area()
        self.assertTrue(w.tool_tabs.isHidden())
        self.assertTrue(w.device_tree.isHidden())


class DeviceTreeMenuTest(unittest.TestCase):
    """どの右クリックメニューにも非表示が出ること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_hide_entry_is_appended_to_any_menu(self):
        from PyQt6.QtWidgets import QMenu
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        menu = QMenu()
        action = tree._add_hide_action(menu)
        self.assertEqual(action.text(), "接続先リストを非表示")
        self.assertIn(action, menu.actions())

    def test_the_console_group_menu_is_no_longer_empty(self):
        """コンソール接続グループでも、隠す道は残すこと。

        以前は何も出さずに帰っていたので、そこを右クリックすると
        メニューが出なかった。
        """
        import inspect
        from ui.device_tree import DeviceTree
        source = inspect.getsource(DeviceTree._show_group_menu)
        self.assertNotIn("return", source.split("コンソール接続")[1][:200],
                         "コンソール接続で早々に帰っている")


if __name__ == "__main__":
    unittest.main()
