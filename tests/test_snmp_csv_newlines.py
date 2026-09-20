"""GET/WALK 結果の CSV が、1 ファイル内で改行コードを混ぜないことを検証する。

_export_results_to_csv は newline="" で開いたファイルへ、見出しの前の注記を
f.write("...\\n") で書いていた。csv.writer は仕様どおり CRLF で行を終える
ので、注記の行だけが LF になる。

実測（生バイト）:
    BOM '# 対象ホスト: 192.0.2.1' 0A 'OID,Type,Value' 0D 0A …
厳しめのパーサは 0A を行の終わりと見ない（CRLF だけを区切りとする）ため、
注記行と見出し行を 1 行と見なしうる。これまでは中断したときだけ出る注記
だったが、対象ホストの注記が入って毎回出るようになった。

直し方: 注記の改行を csv.writer と同じ CRLF にする。
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


class SnmpCsvNewlinesTest(unittest.TestCase):
    window = None

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-csvnl-")
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

    def _raw(self, name, host, reason):
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-csvnl-"), name)
        self.panel._export_results_to_csv(path, RESULTS, host, reason)
        with io.open(path, "rb") as f:
            return f.read()

    def _assert_all_crlf(self, raw):
        """改行がすべて CRLF であること（裸の LF が 1 つも無いこと）"""
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"),
                         "改行コードが混ざっている: %r" % raw[:120])
        self.assertGreater(raw.count(b"\r\n"), 0)

    def test_the_host_note_ends_with_crlf_like_the_rows(self):
        """対象ホストの注記が、結果の行と同じ改行で終わること。"""
        raw = self._raw("results.csv", HOST, None)
        self._assert_all_crlf(raw)
        self.assertIn(("# 対象ホスト: " + HOST + "\r\n"
                       "OID,Type,Value\r\n").encode("utf-8"), raw)

    def test_both_notes_end_with_crlf(self):
        """途中までの注記と対象ホストの注記が、どちらも CRLF で終わること。"""
        raw = self._raw("partial.csv", HOST, "中断")
        self._assert_all_crlf(raw)
        self.assertIn(("# 対象ホスト: " + HOST + "\r\n"
                       "OID,Type,Value\r\n").encode("utf-8"), raw)

    def test_a_csv_without_notes_is_unchanged(self):
        """注記が無いときも、これまでどおり CRLF だけであること。"""
        raw = self._raw("nohost.csv", "", None)
        self._assert_all_crlf(raw)
        # BOM 付き（Excel 対策）のまま、すぐ見出しの行が来る
        self.assertTrue(raw.startswith(b"\xef\xbb\xbfOID,Type,Value\r\n"),
                        repr(raw[:40]))


if __name__ == "__main__":
    unittest.main()
