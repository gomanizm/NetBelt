"""SNMP GET/WALK 結果のエクスポート（txt/csv/json）検証。"""
import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

SAMPLE = [
    ["1.3.6.1.2.1.1.1.0", "OctetString", "Cisco IOS Software"],
    ["1.3.6.1.2.1.1.3.0", "TimeTicks", "123456"],
    ["1.3.6.1.2.1.1.5.0", "OctetString", "router01"],
]


class SNMPExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        # SNMPPanel 単体生成は MIB 読み込みでヘッドレスが落ちるため MainWindow 経由
        from ui.main_window import MainWindow
        return MainWindow().snmp_panel

    def test_export_button_exists(self):
        p = self._panel()
        self.assertTrue(hasattr(p, "export_button"))
        self.assertIn("エクスポート", p.export_button.text())

    def test_export_csv_roundtrip(self):
        p = self._panel()
        path = os.path.join(tempfile.mkdtemp(), "r.csv")
        p._export_results_to_csv(path, SAMPLE, "", None)
        # CSV は BOM 付き（Excel 対策）。utf-8-sig で読む
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["OID", "Type", "Value"])
        self.assertEqual(len(rows), len(SAMPLE) + 1)
        self.assertEqual(rows[1], SAMPLE[0])

    def test_export_json_roundtrip(self):
        p = self._panel()
        path = os.path.join(tempfile.mkdtemp(), "r.json")
        p._export_results_to_json(path, SAMPLE, "", None)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["count"], len(SAMPLE))
        self.assertEqual(data["results"][0]["oid"], SAMPLE[0][0])
        self.assertEqual(data["results"][0]["value"], SAMPLE[0][2])

    def test_export_txt_contains_all_rows(self):
        p = self._panel()
        path = os.path.join(tempfile.mkdtemp(), "r.txt")
        p._export_results_to_txt(path, SAMPLE, "", None)
        text = open(path, encoding="utf-8").read()
        for row in SAMPLE:
            self.assertIn(row[0], text)
            self.assertIn(row[2], text)
        # 改行がリテラル "\n" ではなく実際の改行になっていること
        self.assertNotIn("\\n", text)
        self.assertGreater(len(text.splitlines()), len(SAMPLE))


if __name__ == "__main__":
    unittest.main()
