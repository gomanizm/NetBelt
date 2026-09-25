"""srv-05: Syslog のテキスト保存・コピーで、Unicode の改行が 1 件を 2 行に割る件。

何が起きていたか（実測、基準 470c538。127.0.0.1 のみ）: 本文に
「正常なメッセージ＋区切り文字＋別日時・別 IP・別ホストの偽の記録」を入れた
UDP Syslog を 8 件（区切り文字は U+000B / U+000C / U+001C / U+001D /
U+001E / U+0085 / U+2028 / U+2029 を 1 件ずつ）送ってテキストへ保存すると、
"\\n" で数えれば 8 行なのに str.splitlines() では 16 行になり、偽の記録が
独立した行として並んだ。コピーも同じ（16 行）。_escape_for_export は
バックスラッシュ・CR・LF・TAB だけを置き換えていた。FTP / TFTP / SFTP の
平文ログ（ui/plain_log.py）に入れた改行対策は、この経路に無かった。

どう直したか: テキストの 1 行を作る _export_line（テキスト保存・選択行の
保存・コピーが使う）で、str.splitlines() が行の区切りとみなす残りの文字も
見える表記（\\v \\f \\x1c \\x1d \\x1e \\u0085 \\u2028 \\u2029）へ置き換える。
バックスラッシュは先に二重にしてあるので、元から同じ綴りだった本文とは
区別できる。CSV と JSON は形式が値を囲むので、中身は変えない。
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

BACKSLASH = chr(92)
# str.splitlines() が区切りとみなす文字のうち、改行・復帰（既存の対策）以外。
# 見えない文字をソースへ書かないよう chr() で組み立てる
BREAKS = {
    0x0B: BACKSLASH + "v",
    0x0C: BACKSLASH + "f",
    0x1C: BACKSLASH + "x1c",
    0x1D: BACKSLASH + "x1d",
    0x1E: BACKSLASH + "x1e",
    0x85: BACKSLASH + "u0085",
    0x2028: BACKSLASH + "u2028",
    0x2029: BACKSLASH + "u2029",
}
FAKE = "2026-09-09 03:00:00 192.0.2.9 (UDP/514) core-sw [Emergency] power supply failed"


def forged(code_point):
    return "link down" + chr(code_point) + FAKE


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


class SyslogExportFoldsUnicodeLineBreaksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.syslog_panel import SyslogPanel
        self.panel = SyslogPanel()
        self.dir = tempfile.mkdtemp(prefix="netbelt-syslog-ulb-")

    def _add(self, *messages, hostname="rtr1"):
        for m in messages:
            self.panel.add_message(_Incoming(m, hostname=hostname))
        self.app.processEvents()

    def _run(self, method, filename):
        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=(filename, "")), \
             mock.patch("ui.syslog_panel.QMessageBox.information"), \
             mock.patch("ui.syslog_panel.QMessageBox.critical"), \
             mock.patch("ui.syslog_panel.QMessageBox.warning"):
            method()

    def _read(self, path):
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()

    def test_text_export_keeps_each_message_on_one_line(self):
        self._add(*(forged(cp) for cp in BREAKS))
        out = os.path.join(self.dir, "all.txt")
        self._run(self.panel._export_messages, out)

        lines = self._read(out).splitlines()
        self.assertEqual(len(lines), len(BREAKS),
                         "受信 %d 件が %d 行になっている: %r"
                         % (len(BREAKS), len(lines), lines))
        for line, (cp, shown) in zip(lines, BREAKS.items()):
            with self.subTest(code_point=hex(cp)):
                self.assertTrue(line.startswith("2026-09-09 14:12:02 192.0.2.1"),
                                "受信していない記録が混ざっている: %r" % line)
                self.assertIn("link down" + shown + "2026-09-09 03:00:00", line,
                              "区切り文字が見える表記になっていない")

    def test_save_selected_keeps_each_message_on_one_line(self):
        self._add(forged(0x2028))
        self.panel.table_view.selectRow(0)
        out = os.path.join(self.dir, "sel.txt")
        self._run(self.panel._save_selected, out)

        self.assertEqual(len(self._read(out).splitlines()), 1)

    def test_copy_keeps_each_message_on_one_line(self):
        from PyQt6.QtWidgets import QApplication
        self._add(*(forged(cp) for cp in BREAKS))
        self.panel.table_view.selectAll()
        self.panel._copy_selected()

        lines = QApplication.clipboard().text().splitlines()
        self.assertEqual(len(lines), len(BREAKS),
                         "コピーで 1 件が複数行になっている: %r" % lines)

    def test_a_break_in_the_hostname_does_not_split_the_line(self):
        self._add("link down", hostname="rtr1" + chr(0x2029) + "fake-host")
        out = os.path.join(self.dir, "host.txt")
        self._run(self.panel._export_messages, out)

        self.assertEqual(len(self._read(out).splitlines()), 1)

    def test_a_literal_notation_in_the_body_stays_distinguishable(self):
        """元から「\\u2028」と綴られていた本文が、置き換えた表記と混ざらないこと。"""
        self._add("text " + BACKSLASH + "u2028 end")
        out = os.path.join(self.dir, "literal.txt")
        self._run(self.panel._export_messages, out)

        line = self._read(out).splitlines()[0]
        self.assertTrue(line.endswith("text " + BACKSLASH * 2 + "u2028 end"), line)

    def test_csv_and_json_contents_are_unchanged(self):
        """CSV と JSON は値を囲む形式なので、中身は従来どおりであること。"""
        message = forged(0x2028)
        self._add(message)
        out_csv = os.path.join(self.dir, "all.csv")
        out_json = os.path.join(self.dir, "all.json")
        self._run(self.panel._export_messages, out_csv)
        self._run(self.panel._export_messages, out_json)

        with open(out_csv, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1][4], message)
        with open(out_json, encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["message"], message)


if __name__ == "__main__":
    unittest.main()
