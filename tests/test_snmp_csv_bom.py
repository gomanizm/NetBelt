"""CSV エクスポートが BOM 付き UTF-8 であることを検証する。

GET/WALK 結果と Trap ログの CSV は BOM なし UTF-8 で書かれていた
（実測: 先頭 8 バイトが b"OID,Type" / 日本語見出しの生 UTF-8 で、
EF BB BF が無い）。日本語版 Excel はこれを cp932 として開くため、
見出しの「時刻,送信元IP,...」が「譎ょ綾,騾∽ｿ｡蜈ｵP,...」のように
文字化けする。値は失われないがそのままでは読めない。

txt / json は Excel で開く前提が無いので、BOM を付けない。
"""
import codecs
import csv
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

RESULTS = [
    ["1.3.6.1.2.1.1.5.0", "OctetString", "router01"],
    ["1.3.6.1.2.1.1.6.0", "OctetString", "東京 第1機械室"],
]

TRAPS = [
    {
        "timestamp": "2026-09-12 10:00:00",
        "source_ip": "192.0.2.10",
        "source_port": 162,
        "security": "",
        "trap_oid": "1.3.6.1.4.1.99999.2.0.1",
        "varbinds": [{"oid": "1.3.6.1.2.1.1.5.0", "value": "装置A"}],
    },
]


class SnmpCsvBomTest(unittest.TestCase):
    window = None

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-csvbom-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            cls.window = MainWindow()
        cls.panel = cls.window.snmp_panel

    @classmethod
    def tearDownClass(cls):
        if cls.window is not None:
            cls.window.close()

    def _path(self, name):
        return os.path.join(tempfile.mkdtemp(prefix="netbelt-csvbom-"), name)

    def test_the_result_csv_starts_with_a_utf8_bom(self):
        path = self._path("results.csv")
        self.panel._export_results_to_csv(path, RESULTS, "192.0.2.10", None)
        with open(path, "rb") as f:
            head = f.read(3)
        self.assertEqual(head, codecs.BOM_UTF8,
                         "GET/WALK 結果 CSV に BOM が無く、Excel が cp932 で開く")

    def test_the_trap_csv_starts_with_a_utf8_bom(self):
        path = self._path("traps.csv")
        self.panel._export_to_csv(path, TRAPS)
        with open(path, "rb") as f:
            head = f.read(3)
        self.assertEqual(head, codecs.BOM_UTF8,
                         "Trap CSV に BOM が無く、Excel が cp932 で開く")

    def test_the_result_csv_is_still_readable_as_csv(self):
        """BOM を付けても、見出しと値がそのまま読めること。"""
        path = self._path("results_read.csv")
        self.panel._export_results_to_csv(path, RESULTS, "192.0.2.10", None)
        with io.open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["OID", "Type", "Value"])
        self.assertEqual(rows[1], RESULTS[0])
        self.assertEqual(rows[2][2], "東京 第1機械室")

    def test_the_trap_csv_is_still_readable_as_csv(self):
        path = self._path("traps_read.csv")
        self.panel._export_to_csv(path, TRAPS)
        with io.open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][0], "時刻")
        self.assertEqual(rows[1][1], "192.0.2.10")
        self.assertEqual(rows[1][6], "装置A")

    def test_the_partial_mark_still_comes_before_the_header(self):
        """途中までを示す行は BOM の直後、見出しの前に残ること。"""
        path = self._path("partial.csv")
        self.panel._export_results_to_csv(path, RESULTS, "192.0.2.10", "中断")
        with io.open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        self.assertTrue(lines[0].startswith("# 途中まで"), lines[0])
        self.assertEqual(lines[1], "OID,Type,Value")

    def test_the_text_and_json_exports_keep_no_bom(self):
        """Excel で開かない形式には BOM を足さないこと。"""
        for kind, call in (
            ("txt", lambda p: self.panel._export_results_to_txt(
                p, RESULTS, "192.0.2.10", None)),
            ("json", lambda p: self.panel._export_results_to_json(
                p, RESULTS, "192.0.2.10", None)),
            ("trap_txt", lambda p: self.panel._export_to_txt(p, TRAPS)),
            ("trap_json", lambda p: self.panel._export_to_json(p, TRAPS)),
        ):
            with self.subTest(kind=kind):
                path = self._path(kind + ".out")
                call(path)
                with open(path, "rb") as f:
                    self.assertNotEqual(f.read(3), codecs.BOM_UTF8)


if __name__ == "__main__":
    unittest.main()
