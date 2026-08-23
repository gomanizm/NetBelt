"""タブ式ツールエリアのレイアウト検証（ヘッドレス構築）。"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class TabbedToolAreaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _make(self):
        from ui.main_window import MainWindow
        return MainWindow()

    def test_tool_area_has_six_tabs_in_order(self):
        w = self._make()
        titles = [w.tool_tabs.tabText(i) for i in range(w.tool_tabs.count())]
        self.assertEqual(titles, ["Syslog", "SNMP", "FTP", "TFTP", "SFTPサーバー", "SFTPクライアント"])

    def test_each_tab_wraps_panel_in_scrollarea(self):
        from PyQt6.QtWidgets import QScrollArea
        w = self._make()
        for i in range(w.tool_tabs.count()):
            self.assertIsInstance(w.tool_tabs.widget(i), QScrollArea)
            self.assertTrue(w.tool_tabs.widget(i).widgetResizable())

    def test_splitter_has_three_panes(self):
        w = self._make()
        self.assertEqual(w.main_splitter.count(), 3)

    def test_panels_still_accessible(self):
        w = self._make()
        for attr in ("sftp_panel", "sftp_server_panel", "tftp_server_panel",
                     "ftp_server_panel", "syslog_panel", "snmp_panel"):
            self.assertTrue(hasattr(w, attr))


    def test_menu_action_selects_tab(self):
        w = self._make()
        w._select_tool_tab("ftp_server")
        self.assertEqual(w.tool_tabs.currentIndex(), w._tab_index["ftp_server"])

    def test_toggle_tool_area_hides_and_shows(self):
        w = self._make()
        w._toggle_tool_area()
        self.assertTrue(w.tool_tabs.isHidden())
        w._toggle_tool_area()
        self.assertFalse(w.tool_tabs.isHidden())

    def test_tool_area_has_context_menu(self):
        from PyQt6.QtCore import Qt
        w = self._make()
        self.assertEqual(w.tool_tabs.tabBar().contextMenuPolicy(),
                         Qt.ContextMenuPolicy.CustomContextMenu)
        self.assertTrue(callable(getattr(w, "_tool_area_context_menu", None)))

    def test_layout_save_restore_roundtrip(self):
        from unittest import mock
        w = self._make()
        w.config_manager.save_config = mock.Mock(return_value=True)  # ディスク書込を回避
        w.main_splitter.setSizes([111, 222, 333])
        w.tool_tabs.setCurrentIndex(w._tab_index["snmp"])
        w._save_layout()
        ui = w.config_manager.config["settings"]["ui_layout"]
        self.assertEqual(ui["tool_tab"], w._tab_index["snmp"])
        self.assertEqual(len(ui["splitter_sizes"]), 3)
        w.config_manager.save_config.assert_called_once()
        # 別状態にしてから復元
        w.tool_tabs.setCurrentIndex(0)
        w._restore_layout()
        self.assertEqual(w.tool_tabs.currentIndex(), w._tab_index["snmp"])

    def test_detach_reattach_reparents_single_instance(self):
        from unittest import mock
        w = self._make()
        panel = w.syslog_panel
        scroll = w.tool_tabs.widget(w._tab_index["syslog"])
        self.assertIs(scroll.widget(), panel)
        # offscreen では top-level の show() が落ちるためモックして reparent ロジックのみ検証
        with mock.patch("PyQt6.QtWidgets.QWidget.show"):
            w._detach_tool("syslog")
            self.assertIn("syslog", w._detached)
            self.assertIsNot(scroll.widget(), panel)   # タブはプレースホルダ
            w._reattach_tool("syslog")
        self.assertIs(scroll.widget(), panel)           # 同一インスタンスがタブに戻る
        self.assertNotIn("syslog", w._detached)

if __name__ == "__main__":
    unittest.main()
