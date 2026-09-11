"""エクスポート／選択行保存に送信元・プロトコル/ポート・raw が含まれることを確認する。

画面では「192.0.2.1 (UDP/514)」と「192.0.2.2 (TCP/1514)」で区別できる 2 機器
が、保存後は完全に同一行になっていた。実測: JSON は 2 件とも
{"timestamp", "hostname", "level", "message"} だけ、テキストも 2 行とも
`2026-09-09 14:12:02 rtr1 [Info] link down`。raw も出ないので機器側の日時・
PRI・ヘッダーを復元できない。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Incoming:
    def __init__(self, ip, display, raw):
        self.timestamp = "2026-09-09 14:12:02"
        self.hostname = "rtr1"
        self.level = "Info"
        self.message = "link down"
        self.raw_message = raw
        self.source_ip = ip
        self.source_display = display


class SyslogExportFieldsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.syslog_panel import SyslogPanel
        self.panel = SyslogPanel()
        self.panel.add_message(_Incoming("192.0.2.1", "192.0.2.1 (UDP/514)",
                                         "<134>Sep  9 14:12:02 rtr1 link down"))
        self.panel.add_message(_Incoming("192.0.2.2", "192.0.2.2 (TCP/1514)",
                                         "<134>1 2026-09-09T14:12:02Z rtr1 - - - link down"))
        self.app.processEvents()
        self.dir = tempfile.mkdtemp(prefix="netbelt-syslog-export-")

    def _run(self, method, filename):
        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=(filename, "")), \
             mock.patch("ui.syslog_panel.QMessageBox.information"), \
             mock.patch("ui.syslog_panel.QMessageBox.critical"), \
             mock.patch("ui.syslog_panel.QMessageBox.warning"):
            method()

    def test_json_export_carries_source_and_raw(self):
        out = os.path.join(self.dir, "all.json")
        self._run(self.panel._export_messages, out)
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 2)
        for rec, src, raw in zip(data, ("192.0.2.1 (UDP/514)", "192.0.2.2 (TCP/1514)"),
                                 ("<134>Sep  9 14:12:02 rtr1 link down",
                                  "<134>1 2026-09-09T14:12:02Z rtr1 - - - link down")):
            self.assertEqual(rec.get("source"), src, "送信元が無い: %r" % rec)
            self.assertEqual(rec.get("raw"), raw, "raw が無い: %r" % rec)
            for k in ("timestamp", "hostname", "level", "message"):
                self.assertIn(k, rec)

    def test_text_export_distinguishes_the_two_sources(self):
        out = os.path.join(self.dir, "all.txt")
        self._run(self.panel._export_messages, out)
        with open(out, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("192.0.2.1 (UDP/514)", lines[0])
        self.assertIn("192.0.2.2 (TCP/1514)", lines[1])
        self.assertNotEqual(lines[0], lines[1], "別機器の行が同一になっている")

    def test_save_selected_text_includes_the_source(self):
        self.panel.table_view.selectRow(1)
        out = os.path.join(self.dir, "sel.txt")
        self._run(self.panel._save_selected, out)
        with open(out, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("192.0.2.2 (TCP/1514)", content, "送信元が無い: %r" % content)
        self.assertIn("link down", content)


    def test_copy_selected_distinguishes_the_two_sources(self):
        from PyQt6.QtWidgets import QApplication
        self.panel.table_view.selectAll()
        self.panel._copy_selected()
        text = QApplication.clipboard().text()
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("192.0.2.1 (UDP/514)", text, "送信元が無い: %r" % text)
        self.assertIn("192.0.2.2 (TCP/1514)", text, "送信元が無い: %r" % text)
        self.assertNotEqual(lines[0], lines[1], "別機器の行が同一になっている")


if __name__ == "__main__":
    unittest.main()
