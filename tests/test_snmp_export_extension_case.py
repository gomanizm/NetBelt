"""エクスポート形式の判定が拡張子の大文字小文字に引きずられないことを検証する。

_on_export_clicked / _on_trap_export_clicked は file_path.endswith('.csv')
'.json' で形式を決め、当たらなければ TXT を書いていた。endswith は大小を
区別するので、out.CSV は TXT の中身で書かれ、しかも「エクスポートしました」
と成功扱いになる。

ファイルダイアログはフィルタに従って拡張子を小文字で補うので、ここへ来る
のは利用者が自分で .CSV / .JSON と打ったときと、大文字名の既存ファイルを
選び直したとき。データは失われず開けば読めるが、中身と拡張子が食い違った
ファイルが「成功」として残る。Excel や機械処理へ渡す側が気づけない。

判定は os.path.splitext()[1].lower() で行う。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

ROWS = [
    ("1.3.6.1.2.1.1.1.0", "OctetString", "row-1"),
    ("1.3.6.1.2.1.1.5.0", "OctetString", "row-2"),
]
HOST = "192.0.2.10"

TRAP = {
    "source_ip": "192.0.2.20",
    "source_port": 162,
    "trap_oid": "1.3.6.1.4.1.9.9.41.2.0.1",
    "received_at": "2026-01-02T03:04:05",
    "varbinds": [{"oid": "1.3.6.1.2.1.1.5.0", "value": "device-01"}],
}


class SnmpExportExtensionCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作ったウィンドウはクラス終了まで保持する（test_snmp_trap_limit と同じ）
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _panel(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-ext-case-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        return window.snmp_panel

    def _export(self, panel, handler, name):
        """保存ダイアログで name を選んだことにして書き出し、中身を返す。"""
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-ext-case-"), name)
        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
             mock.patch("ui.snmp_panel.QMessageBox"):
            handler()
        with io.open(path, encoding="utf-8-sig") as f:
            return f.read()

    def _results_panel(self):
        panel = self._panel()
        panel.result_model.set_results(list(ROWS))
        panel._result_host = HOST
        return panel

    def _traps_panel(self):
        panel = self._panel()
        panel._add_trap_to_tree(dict(TRAP))
        return panel

    # --- GET/WALK 結果 ---------------------------------------------------

    def test_results_saved_as_uppercase_csv_are_csv(self):
        panel = self._results_panel()
        text = self._export(panel, panel._on_export_clicked, "out.CSV")
        self.assertTrue(text.startswith("OID,Type,Value"),
                        "out.CSV が CSV になっていない: %r" % text[:40])

    def test_results_saved_as_uppercase_json_are_json(self):
        panel = self._results_panel()
        text = self._export(panel, panel._on_export_clicked, "out.JSON")
        data = json.loads(text)
        self.assertEqual([r["value"] for r in data["results"]],
                         [r[2] for r in ROWS])

    def test_results_saved_as_mixed_case_csv_are_csv(self):
        panel = self._results_panel()
        text = self._export(panel, panel._on_export_clicked, "out.Csv")
        self.assertTrue(text.startswith("OID,Type,Value"),
                        "out.Csv が CSV になっていない: %r" % text[:40])

    def test_results_saved_as_lowercase_csv_still_are_csv(self):
        """小文字の従来どおりの経路を壊していないこと。"""
        panel = self._results_panel()
        text = self._export(panel, panel._on_export_clicked, "out.csv")
        self.assertTrue(text.startswith("OID,Type,Value"))

    def test_results_saved_as_txt_still_are_text(self):
        """既定の txt（および未知の拡張子）はこれまでどおりテキスト。"""
        panel = self._results_panel()
        text = self._export(panel, panel._on_export_clicked, "out.txt")
        self.assertIn("SNMP GET/WALK 結果", text)

    def test_results_saved_with_csv_only_inside_the_name_stay_text(self):
        """名前の途中の .csv で CSV にしないこと（拡張子は末尾のみ）。"""
        panel = self._results_panel()
        text = self._export(panel, panel._on_export_clicked, "out.csv.txt")
        self.assertIn("SNMP GET/WALK 結果", text)

    # --- Trap ログ -------------------------------------------------------

    def test_traps_saved_as_uppercase_csv_are_csv(self):
        panel = self._traps_panel()
        text = self._export(panel, panel._on_trap_export_clicked, "trap.CSV")
        self.assertTrue(text.startswith("時刻,"),
                        "trap.CSV が CSV になっていない: %r" % text[:40])

    def test_traps_saved_as_uppercase_json_are_json(self):
        panel = self._traps_panel()
        text = self._export(panel, panel._on_trap_export_clicked, "trap.JSON")
        data = json.loads(text)
        self.assertEqual(data[0]["source_ip"], TRAP["source_ip"])

    def test_traps_saved_as_txt_still_are_text(self):
        panel = self._traps_panel()
        text = self._export(panel, panel._on_trap_export_clicked, "trap.txt")
        self.assertIn("SNMP Trap Log", text)


if __name__ == "__main__":
    unittest.main()
