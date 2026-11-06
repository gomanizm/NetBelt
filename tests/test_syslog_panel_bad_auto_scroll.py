# -*- coding: utf-8 -*-
"""config.json の settings.syslog.auto_scroll が bool でなくてもパネルが作れること。

auto_scroll を型を見ずに QCheckBox.setChecked へ渡していたので、手編集で
"true"（文字列）や null を書くと TypeError で SyslogPanel（MainWindow）の
構築が失敗し、NetBelt が起動しなかった。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _write_config(path, syslog_section):
    config = {
        "config_version": "1.0",
        "groups": [{"name": "Default", "auto_commands": [], "devices": []}],
        "global_macros": [],
        "settings": {"syslog": syslog_section},
        "update_settings": {"check_on_startup": False},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f)


class SyslogPanelBadAutoScrollTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()
        cls._keep.clear()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-bad-autoscroll-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def _panel(self, value):
        from core.config_manager import ConfigManager
        from ui.syslog_panel import SyslogPanel
        _write_config(self.path, {"auto_scroll": value, "max_messages": 1000})
        panel = SyslogPanel(config_manager=ConfigManager(config_path=self.path))
        self._keep.append(panel)
        return panel

    def test_non_bool_values_fall_back_to_the_default(self):
        for value in ("true", "false", "yes", "", None, [], {}, 1.5):
            with self.subTest(value=value):
                panel = self._panel(value)
                self.assertIs(panel.auto_scroll, True)
                self.assertTrue(panel.auto_scroll_checkbox.isChecked())

    def test_bool_values_are_kept(self):
        for value in (True, False):
            with self.subTest(value=value):
                panel = self._panel(value)
                self.assertIs(panel.auto_scroll, value)
                self.assertEqual(panel.auto_scroll_checkbox.isChecked(), value)

    def test_integers_keep_their_previous_meaning(self):
        """0 / 1 は従来も setChecked が受けていたので、意味を変えない"""
        for value, expected in ((0, False), (1, True)):
            with self.subTest(value=value):
                panel = self._panel(value)
                self.assertIs(panel.auto_scroll, expected)
                self.assertEqual(panel.auto_scroll_checkbox.isChecked(), expected)


if __name__ == "__main__":
    unittest.main()
