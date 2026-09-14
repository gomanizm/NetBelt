"""端末以外の右クリックメニューも、親の子として積み上がらないことを検証する。

端末（InteractiveTerminal）は閉じたメニューを捨てるようになったが、同じ
QMenu(self) の作り方をしている隣のパネルは手付かずだった。接続先リスト
（機器・グループ・空欄の 3 か所）、SFTP クライアント、Syslog、ツールエリアの
タブバーは、右クリックのたびに QMenu 1 件と項目が親の子として残る
（実測: 空欄メニューを 5 回開いて QMenu 5 件 / QAction 20 件）。

開き終えたメニューは項目ごと破棄されて、繰り返しても増えないこと。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class PanelContextMenusReleasedTest(unittest.TestCase):
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

    @staticmethod
    def _counts(widget):
        """widget の子になっている QMenu / QAction の件数を返す。"""
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import QMenu
        return (len(widget.findChildren(QMenu)),
                len(widget.findChildren(QAction)))

    def _repeat(self, widget, open_menu, times=5):
        """メニューを times 回開閉して、子の増分を返す。

        Returns:
            (QMenu の増分, QAction の増分)
        """
        opened = {"items": 0}

        def fake_exec(menu, *args, **kwargs):
            opened["items"] = max(opened["items"], len(menu.actions()))
            return None

        self._pump()
        before = self._counts(widget)
        with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
            for _ in range(times):
                open_menu()
        self._pump()
        after = self._counts(widget)
        self.assertGreater(opened["items"], 0,
                           "前提: 項目のあるメニューが開いている")
        return (after[0] - before[0], after[1] - before[1])

    def _assert_no_pile_up(self, widget, open_menu):
        menus, actions = self._repeat(widget, open_menu)
        self.assertEqual((menus, actions), (0, 0),
                         "閉じたメニューが残っている: QMenu %d 件 / QAction %d 件"
                         % (menus, actions))

    # ----- 接続先リスト -----

    def _device_tree(self):
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        self.addCleanup(tree.close)
        tree.load_from_config([
            {"name": "Lab", "devices": [
                {"name": "rtrA", "host": "192.0.2.10", "protocol": "ssh"},
            ]},
        ])
        return tree

    @staticmethod
    def _item_pos(tree, item):
        """ツリー項目の上を指す viewport 座標を返す。"""
        return tree.tree.visualItemRect(item).center()

    def test_device_tree_empty_area_menu_does_not_pile_up(self):
        from PyQt6.QtCore import QPoint
        tree = self._device_tree()
        self._assert_no_pile_up(
            tree, lambda: tree._show_empty_area_menu(QPoint(1, 1)))

    def test_device_tree_group_menu_does_not_pile_up(self):
        tree = self._device_tree()
        group = tree.tree.topLevelItem(0)
        self._assert_no_pile_up(
            tree, lambda: tree._show_group_menu(
                self._item_pos(tree, group), group))

    def test_device_tree_device_menu_does_not_pile_up(self):
        tree = self._device_tree()
        device = tree.tree.topLevelItem(0).child(0)
        pos = self._item_pos(tree, device)
        self.assertIs(tree.tree.itemAt(pos), device,
                      "前提: 機器の上を指している")
        self._assert_no_pile_up(tree, lambda: tree._show_context_menu(pos))

    # ----- SFTP クライアント -----

    def test_sftp_panel_menu_does_not_pile_up(self):
        from PyQt6.QtCore import QPoint
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        self.addCleanup(panel.close)
        manager = mock.Mock()
        manager.get_current_path.return_value = "/"
        panel.sftp_manager = manager
        self._assert_no_pile_up(
            panel, lambda: panel._show_context_menu(QPoint(1, 1)))

    # ----- Syslog -----

    def test_syslog_panel_menu_does_not_pile_up(self):
        from PyQt6.QtCore import QPoint
        from ui.syslog_panel import SyslogPanel
        panel = SyslogPanel()
        self.addCleanup(panel.close)
        self._assert_no_pile_up(
            panel, lambda: panel._show_context_menu(QPoint(1, 1)))

    # ----- ツールエリアのタブバー -----

    def test_tool_area_tab_menu_does_not_pile_up(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        bar = window.tool_tabs.tabBar()
        pos = bar.tabRect(0).center()
        self._assert_no_pile_up(
            window, lambda: window._tool_area_context_menu(pos))


if __name__ == "__main__":
    unittest.main()
