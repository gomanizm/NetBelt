"""config.json の settings や settings.syslog が null（dict でない）でも起動できること。

SyslogPanel._load_config は settings と settings.syslog が dict だと決めつけて
.get を呼んでいた。config.json は手で編集できるので null や文字列が入りうる。
MainWindow は SyslogPanel を保護なしで組むので、構築そのものが失敗した。
実測: settings.syslog が null でも settings が null でも、MainWindow() が
AttributeError: 'NoneType' object has no attribute 'get' で失敗し、アプリが
起動しなかった。使っていない _get_listen_port も同じ形だった。
他のパネルは、サーバーの各パネルが ConfigManager.get_server_settings（dict を
保証する）を使い、SNMP パネルと MainWindow のレイアウト復元は例外を受けて
既定値に戻るので、同じ形で落ちるところは無かった。

直し方: SyslogPanel に settings.syslog を dict で返す手段を 1 つ置き、
dict でなければ既定値（空の設定）として扱う。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _write_config(path, settings):
    config = {
        "config_version": "1.0",
        "groups": [{"name": "Default", "auto_commands": [], "devices": []}],
        "global_macros": [],
        "settings": settings,
        "update_settings": {"check_on_startup": False},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f)


class SyslogPanelNullSettingsTest(unittest.TestCase):
    _keep = []   # 配送待ちシグナルの宛先を先に解放しない（他テストと同じ理由）

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
        self.dir = tempfile.mkdtemp(prefix="netbelt-null-settings-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)
        # MainWindow が作業フォルダへ何か書いても、この一時フォルダに収める
        prev = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, prev)

    def _config_manager(self, settings):
        from core.config_manager import ConfigManager
        _write_config(self.path, settings)
        return ConfigManager(config_path=self.path)

    def _main_window(self, settings):
        from ui.main_window import MainWindow
        cm = self._config_manager(settings)
        with mock.patch("ui.main_window.ConfigManager", return_value=cm):
            w = MainWindow()
        self._keep.append(w)
        return w

    def test_main_window_builds_when_the_syslog_section_is_null(self):
        w = self._main_window({"syslog": None})
        self.assertEqual(w.syslog_panel.max_messages, 1000)
        self.assertTrue(w.syslog_panel.auto_scroll)

    def test_main_window_builds_when_settings_is_null(self):
        w = self._main_window(None)
        self.assertEqual(w.syslog_panel.max_messages, 1000)

    def test_panel_uses_defaults_for_non_dict_sections(self):
        from ui.syslog_panel import SyslogPanel
        for settings in (None, "broken", {"syslog": None}, {"syslog": ["x"]}):
            with self.subTest(settings=settings):
                panel = SyslogPanel(config_manager=self._config_manager(settings))
                self._keep.append(panel)
                self.assertEqual(panel.max_messages, 1000)
                self.assertTrue(panel.auto_scroll)
                self.assertEqual(panel._get_listen_port(), 514)

    def test_values_in_a_proper_section_are_still_used(self):
        from ui.syslog_panel import SyslogPanel
        panel = SyslogPanel(config_manager=self._config_manager(
            {"syslog": {"max_messages": 250, "auto_scroll": False,
                        "listen_port": 1514}}))
        self._keep.append(panel)
        self.assertEqual(panel.max_messages, 250)
        self.assertFalse(panel.auto_scroll)
        self.assertEqual(panel._get_listen_port(), 1514)


if __name__ == "__main__":
    unittest.main()
