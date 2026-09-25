"""GET/WALK 結果の CSV にも対象ホストが残ることを検証する。

_export_results_to_csv は引数 host を受け取るのに一度も書いていなかった。
JSON は "host"、TXT は「対象ホスト:」を書くので、CSV だけが抜けている。
実測（192.0.2.1 へ GET/WALK して CSV に書き出す）: 中身は `OID,Type,Value`
の見出しと結果の行だけで、どの機器から採った結果か分からない。複数の
機器を続けて見たあとでファイルを並べると、取り違えても気づけない。

直し方: 途中まで（中断）の注記と同じ形で、見出しの前に
`# 対象ホスト: <host>` を 1 行足す。注記と同じく、値が無いとき
（host が空文字）は行ごと書かない。JSON・TXT の書き方は変えない。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

HOST = "192.0.2.1"
RESULTS = [
    ["1.3.6.1.2.1.1.5.0", "OctetString", "router01"],
    ["1.3.6.1.2.1.1.6.0", "OctetString", "東京 第1機械室"],
]


class SnmpCsvRecordsHostTest(unittest.TestCase):
    window = None

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-csvhost-")
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

    def _write(self, name, host, reason):
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-csvhost-"), name)
        self.panel._export_results_to_csv(path, RESULTS, host, reason)
        # CSV は BOM 付き（Excel 対策）
        with io.open(path, encoding="utf-8-sig") as f:
            return f.read().splitlines()

    def test_the_result_csv_names_the_host_before_the_header(self):
        """完走した結果の CSV が、見出しの前に対象ホストを書くこと。"""
        lines = self._write("results.csv", HOST, None)
        self.assertEqual(lines[0], "# 対象ホスト: " + HOST,
                         "どの機器の結果か分からない: %r" % lines[:2])
        self.assertEqual(lines[1], "OID,Type,Value")

    def test_a_partial_csv_keeps_both_notes_before_the_header(self):
        """途中までの結果でも、注記と対象ホストが見出しの前に並ぶこと。"""
        lines = self._write("partial.csv", HOST, "中断")
        self.assertTrue(lines[0].startswith("# 途中まで"), lines[0])
        self.assertEqual(lines[1], "# 対象ホスト: " + HOST)
        self.assertEqual(lines[2], "OID,Type,Value")

    def test_an_empty_host_writes_no_line(self):
        """ホストが空のときは、空の注記を書かないこと。"""
        lines = self._write("nohost.csv", "", None)
        self.assertEqual(lines[0], "OID,Type,Value")


if __name__ == "__main__":
    unittest.main()
