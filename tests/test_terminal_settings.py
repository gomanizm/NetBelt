"""ターミナルの外観設定（settings.terminal）の適用。"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class TerminalSettingsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        return TerminalWidget()

    def test_defaults_match_the_shipped_config(self):
        from ui.terminal_widget import TerminalWidget
        self.assertEqual(TerminalWidget.DEFAULT_TERMINAL_SETTINGS, {
            "background_color": "#000000",
            "text_color": "#FFFFFF",
            "font_family": "Consolas",
            "font_size": 10,
        })

    def test_apply_changes_the_home_tab(self):
        """ホームタブは _terminals に入らないので取り残されやすい。"""
        from PyQt6.QtGui import QColor, QPalette
        w = self._widget()
        w.apply_terminal_settings({
            "background_color": "#102030", "text_color": "#A0B0C0",
            "font_family": "Courier New", "font_size": 14})
        home = w.tab_widget.widget(0)
        self.assertEqual(home.font().family(), "Courier New")
        self.assertEqual(home.font().pointSize(), 14)
        self.assertEqual(home.palette().color(QPalette.ColorRole.Base),
                         QColor("#102030"))
        self.assertEqual(home.palette().color(QPalette.ColorRole.Text),
                         QColor("#A0B0C0"))

    def test_apply_changes_existing_connected_tabs(self):
        w = self._widget()
        w.create_terminal_tab("ルータA")
        w.create_terminal_tab("ルータB")
        w.apply_terminal_settings({"font_family": "Courier New", "font_size": 16})
        for i in range(w.tab_widget.count()):
            self.assertEqual(w.tab_widget.widget(i).font().pointSize(), 16)

    def test_tabs_created_after_apply_inherit_the_settings(self):
        w = self._widget()
        w.apply_terminal_settings({"font_size": 18})
        term = w.create_terminal_tab("あとから接続")
        self.assertEqual(term.font().pointSize(), 18)

    def test_missing_keys_fall_back_to_defaults(self):
        w = self._widget()
        w.apply_terminal_settings({"font_size": 20})
        home = w.tab_widget.widget(0)
        self.assertEqual(home.font().family(), "Consolas")
        self.assertEqual(home.font().pointSize(), 20)

    def test_get_current_terminal_returns_the_visible_widget(self):
        w = self._widget()
        self.assertIs(w.get_current_terminal(), w.tab_widget.widget(0))
        term = w.create_terminal_tab("ルータA")
        self.assertIs(w.get_current_terminal(), term)


if __name__ == "__main__":
    unittest.main()
