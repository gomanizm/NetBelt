"""config.json の settings.syslog.max_messages が無限大でも起動と受信で落ちないこと。

実測（基準 441ea02）: config.json の settings.syslog.max_messages に 1e309 /
-1e309 / Infinity を手で書くと、json はそれを float の inf / -inf に読む。
SyslogPanel を作ると _load_config -> _sanitize_max_messages の int(value) が
OverflowError: cannot convert float infinity to integer を投げ、受けているのが
(TypeError, ValueError) だけなので抜けていた。SyslogPanel は MainWindow の
構築中に作られるので、この値が残っている限りアプリが起動しない
（MainWindow() でも同じ例外を確認）。NaN は int() が ValueError なので既定の
1000 へ戻っていた。SyslogTableModel._limit_reached の int(self.max_messages)
も同じ形で、モデルへ直接 inf を渡すと最初の受信で同じ例外になった。

直し方: 両方の except に OverflowError を足す。無限大は他の使えない値と同じく
既定値 1000 へ戻り、モデルの上限判定では「上限に達していない」扱いになる
（tests/test_syslog_panel_bad_max_messages.py の不正値の扱いと同じ方向）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _config_text(literal):
    """max_messages に literal（JSON の字面そのまま）を書いた config.json の中身"""
    return ('{"config_version": "1.0", "groups": [{"name": "Default", '
            '"auto_commands": [], "devices": []}], "global_macros": [], '
            '"settings": {"syslog": {"max_messages": %s}}, '
            '"update_settings": {"check_on_startup": false}}' % literal)


class SyslogPanelInfiniteMaxMessagesTest(unittest.TestCase):
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
        self.dir = tempfile.mkdtemp(prefix="netbelt-inf-max-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def _panel(self, literal):
        from core.config_manager import ConfigManager
        from ui.syslog_panel import SyslogPanel
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(_config_text(literal))
        cm = ConfigManager(config_path=self.path)
        panel = SyslogPanel(config_manager=cm)
        self._keep.append(panel)
        return cm, panel

    def test_infinite_max_messages_falls_back_to_the_default(self):
        for literal in ("1e309", "-1e309", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                cm, panel = self._panel(literal)
                raw = cm.config["settings"]["syslog"]["max_messages"]
                # 前提: json は無限大の float として読んでいる
                self.assertIsInstance(raw, float)
                self.assertEqual(abs(raw), float("inf"))
                self.assertEqual(panel.max_messages, 1000)
                self.assertEqual(panel.model.max_messages, 1000)

    def test_panel_still_receives_after_an_infinite_limit(self):
        from core.syslog_receiver import SyslogMessage
        _cm, panel = self._panel("1e309")
        for _ in range(3):
            panel.add_message(SyslogMessage(
                "<190>Sep 20 10:00:00 rtr1: link up", "192.0.2.1",
                proto="UDP", port=514))
        self.assertEqual(len(panel.model.messages), 3)
        self.assertEqual(panel.model.rowCount(), 3)

    def test_model_with_an_infinite_limit_keeps_receiving(self):
        """モデルへ直接 inf を渡しても受信スロットから例外を出さない"""
        from ui.syslog_panel import SyslogMessage, SyslogTableModel
        for limit in (float("inf"), float("-inf")):
            with self.subTest(limit=limit):
                model = SyslogTableModel(limit)
                self._keep.append(model)
                for i in range(3):
                    model.add_message(SyslogMessage("t", "rtr1", "Info",
                                                    "m%d" % i, "raw", "192.0.2.1"))
                # 数として使える上限が無いので件数で捨てない
                self.assertEqual(len(model.messages), 3)
                self.assertEqual(model.rowCount(), 3)

    def test_json_dump_of_inf_is_read_back_as_inf(self):
        """_write_config（json.dump）経由でも同じ値になる（既存テストの書き方）"""
        from core.config_manager import ConfigManager
        from ui.syslog_panel import SyslogPanel
        config = {
            "config_version": "1.0",
            "groups": [{"name": "Default", "auto_commands": [], "devices": []}],
            "global_macros": [],
            "settings": {"syslog": {"max_messages": float("inf")}},
            "update_settings": {"check_on_startup": False},
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(config, f)
        panel = SyslogPanel(config_manager=ConfigManager(config_path=self.path))
        self._keep.append(panel)
        self.assertEqual(panel.max_messages, 1000)


if __name__ == "__main__":
    unittest.main()
