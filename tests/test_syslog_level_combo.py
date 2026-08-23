"""Syslog レベルフィルタのチェック可能ドロップダウン（CheckableComboBox）の検証。"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class CheckableComboTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_add_and_checked_items(self):
        from ui.syslog_panel import CheckableComboBox
        c = CheckableComboBox()
        for lv in ["Error", "Warning", "Info"]:
            c.add_checkable(lv, True)
        self.assertEqual(c.checked_items(), {"Error", "Warning", "Info"})

    def test_uncheck_updates_items(self):
        from PyQt6.QtCore import Qt
        from ui.syslog_panel import CheckableComboBox
        c = CheckableComboBox()
        c.add_checkable("Error", True)
        c.add_checkable("Debug", True)
        c.model().item(1).setData(Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        self.assertEqual(c.checked_items(), {"Error"})

    def test_syslog_panel_uses_level_combo(self):
        from ui.syslog_panel import SyslogPanel, CheckableComboBox
        p = SyslogPanel()
        self.assertIsInstance(p.level_combo, CheckableComboBox)
        # 既定で全8レベルが選択済み
        self.assertEqual(len(p.level_combo.checked_items()), 8)


    def test_default_port_is_514(self):
        from ui.syslog_panel import SyslogPanel
        p = SyslogPanel()
        self.assertEqual(p.udp_port_spin.value(), 514)
        self.assertEqual(p.tcp_port_spin.value(), 514)

if __name__ == "__main__":
    unittest.main()
