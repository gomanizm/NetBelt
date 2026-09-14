"""本文の改行が保存・コピーで偽の記録行にならないことを検証する。

_export_line は
`{timestamp} {source_ip} {hostname} [{level}] {message}` を組み立てるだけで、
message の改行をそのまま書いていた。認証なしで届く 1 件の Syslog に
改行＋別日時・別 IP・別ホスト・別レベルを入れておくと、保存したテキストや
コピーした内容では、その続きが別機器の独立した記録として並ぶ。
画面のテーブルでは 1 行のままなので、保存物と画面を突き合わせても気づけない。

1 件は必ず 1 行に収める（改行・復帰・タブはエスケープ表記にする）。
JSON 保存は json.dump がエスケープするので、そちらは従来どおり。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 受信側が入れてくる偽装本文。続く行が別機器の記録に見える書式になっている。
FORGED = ("link down\n"
          "2026-09-09 03:00:00 192.0.2.9 (UDP/514) core-sw [Emergency] "
          "power supply failed")


class _Incoming:
    """受信側 SyslogMessage の最小形（panel.add_message が読む属性だけ）。"""

    def __init__(self, message, hostname="rtr1"):
        self.timestamp = "2026-09-09 14:12:02"
        self.hostname = hostname
        self.level = "Info"
        self.message = message
        self.raw_message = "<134>Sep  9 14:12:02 rtr1 " + message
        self.source_ip = "192.0.2.1"
        self.source_display = "192.0.2.1 (UDP/514)"


class SyslogExportEscapesNewlinesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.syslog_panel import SyslogPanel
        self.panel = SyslogPanel()
        self.dir = tempfile.mkdtemp(prefix="netbelt-syslog-escape-")

    def _add(self, *messages):
        for m in messages:
            self.panel.add_message(_Incoming(m))
        self.app.processEvents()

    def _run(self, method, filename):
        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=(filename, "")), \
             mock.patch("ui.syslog_panel.QMessageBox.information"), \
             mock.patch("ui.syslog_panel.QMessageBox.critical"), \
             mock.patch("ui.syslog_panel.QMessageBox.warning"):
            method()

    def _read_lines(self, path):
        with open(path, encoding="utf-8", newline="") as f:
            return f.read().splitlines()

    def test_text_export_keeps_one_message_on_one_line(self):
        self._add(FORGED)
        out = os.path.join(self.dir, "all.txt")
        self._run(self.panel._export_messages, out)

        lines = self._read_lines(out)
        self.assertEqual(len(lines), 1,
                         "1 件の受信が複数行の記録になっている: %r" % lines)
        self.assertIn("\\n", lines[0], "改行がエスケープされていない")

    def test_text_export_keeps_the_line_count_equal_to_the_message_count(self):
        self._add("link up", FORGED, "link down")
        out = os.path.join(self.dir, "three.txt")
        self._run(self.panel._export_messages, out)

        lines = self._read_lines(out)
        self.assertEqual(len(lines), 3,
                         "受信 3 件が %d 行になっている: %r" % (len(lines), lines))
        for line in lines:
            self.assertTrue(line.startswith("2026-09-09 14:12:02 192.0.2.1 (UDP/514)"),
                            "受信していない記録が混ざっている: %r" % line)

    def test_save_selected_keeps_one_message_on_one_line(self):
        self._add(FORGED)
        self.panel.table_view.selectRow(0)
        out = os.path.join(self.dir, "sel.txt")
        self._run(self.panel._save_selected, out)

        lines = self._read_lines(out)
        self.assertEqual(len(lines), 1,
                         "1 件の受信が複数行の記録になっている: %r" % lines)

    def test_copy_selected_keeps_one_message_on_one_line(self):
        from PyQt6.QtWidgets import QApplication
        self._add(FORGED)
        self.panel.table_view.selectAll()
        self.panel._copy_selected()

        lines = QApplication.clipboard().text().splitlines()
        self.assertEqual(len(lines), 1,
                         "1 件の受信が複数行の記録になっている: %r" % lines)

    def test_carriage_return_and_tab_are_escaped_too(self):
        self._add("first\r\nsecond\tthird")
        out = os.path.join(self.dir, "cr.txt")
        self._run(self.panel._export_messages, out)

        lines = self._read_lines(out)
        self.assertEqual(len(lines), 1, "CR で行が割れている: %r" % lines)
        self.assertIn("first\\r\\nsecond\\tthird", lines[0])

    def test_hostname_newline_does_not_split_the_line(self):
        self.panel.add_message(_Incoming("link down", hostname="rtr1\nfake-host"))
        self.app.processEvents()
        out = os.path.join(self.dir, "host.txt")
        self._run(self.panel._export_messages, out)

        lines = self._read_lines(out)
        self.assertEqual(len(lines), 1,
                         "ホスト名の改行で行が割れている: %r" % lines)

    def test_a_backslash_n_in_the_body_stays_distinguishable(self):
        """元から「\\n」と書かれていた本文が、改行由来の表記と混ざらないこと。"""
        self._add("path is C:\\ntest")
        out = os.path.join(self.dir, "bs.txt")
        self._run(self.panel._export_messages, out)

        lines = self._read_lines(out)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("path is C:\\\\ntest"),
                        "本文のバックスラッシュが改行の表記と区別できない: %r" % lines[0])

    def test_json_export_still_carries_the_raw_message(self):
        """JSON は json.dump がエスケープするので、本文は受信したまま残ること。"""
        self._add(FORGED)
        out = os.path.join(self.dir, "all.json")
        self._run(self.panel._export_messages, out)

        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data[0]["message"], FORGED)


if __name__ == "__main__":
    unittest.main()
