"""タブの並べ替え（ドラッグ）と、その並び順の保存/復元を検証する。"""
import os
import sys
import unittest

sys.path.insert(0, "src")

DEFAULT_ORDER = ["syslog", "snmp", "ftp_server", "tftp_server", "sftp_server", "sftp"]


class TabReorderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _win(self):
        from ui.main_window import MainWindow
        return MainWindow()

    def test_tabs_are_movable(self):
        w = self._win()
        self.assertTrue(w.tool_tabs.isMovable())

    def test_default_order(self):
        w = self._win()
        self.assertEqual(w._tool_order(), DEFAULT_ORDER)

    def test_key_mapping_survives_reorder(self):
        # 並べ替えてもキー -> タブ位置の対応が追従すること（デタッチ先を間違えない）
        w = self._win()
        w.tool_tabs.tabBar().moveTab(0, 5)          # 先頭(Syslog)を末尾へ
        order = w._tool_order()
        self.assertEqual(order[-1], "syslog")
        for key in DEFAULT_ORDER:
            idx = w._tab_index[key]
            self.assertIs(w.tool_tabs.widget(idx), w._tool_scrolls[key])
            self.assertEqual(w._tool_key_at(idx), key)

    def test_detach_targets_correct_tool_after_reorder(self):
        from unittest import mock
        w = self._win()
        w.tool_tabs.tabBar().moveTab(0, 5)          # 並びを入れ替える
        key = "tftp_server"
        idx = w._tab_index[key]
        panel = w._tool_scrolls[key].widget()
        with mock.patch("PyQt6.QtWidgets.QWidget.show"):
            w._detach_tool_by_index(idx)            # ドラッグ経路と同じ入口
            self.assertIn(key, w._detached, "並べ替え後に別のツールがデタッチされた")
            self.assertIs(w._detached[key].layout().itemAt(0).widget(), panel)
            w._reattach_tool(key)

    def test_order_saved_and_restored(self):
        from unittest import mock
        w = self._win()
        w.config_manager.save_config = mock.Mock(return_value=True)
        w.tool_tabs.tabBar().moveTab(0, 5)
        expected = w._tool_order()
        w._save_layout()
        saved = w.config_manager.config["settings"]["ui_layout"]["tool_order"]
        self.assertEqual(saved, expected)

        # 保存済みの順序が新しいウィンドウで復元されること
        w2 = self._win()
        w2.config_manager.config.setdefault("settings", {})["ui_layout"] = {"tool_order": expected}
        w2._restore_tab_order()
        self.assertEqual(w2._tool_order(), expected)

    def test_restore_ignores_broken_order(self):
        w = self._win()
        w.config_manager.config.setdefault("settings", {})["ui_layout"] = {
            "tool_order": ["nonexistent", "snmp"]     # 未知キー混じりでも壊れない
        }
        w._restore_tab_order()
        self.assertEqual(sorted(w._tool_order()), sorted(DEFAULT_ORDER))
        self.assertEqual(w._tool_order()[0], "snmp")


if __name__ == "__main__":
    unittest.main()
