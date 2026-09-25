"""エクスポートの拡張子が、ダイアログで選んだ種類に付いてくることを検証する。

実測（1a206c2）: 既定のファイル名は "....txt" で固定され、戻り値の
selectedFilter は捨てていた（snmp_panel.py の _on_export_clicked は
`file_path, _ =`、_on_trap_export_clicked は selected_filter を受け取り
ながら一度も読まない）。種類を「CSVファイル (*.csv)」へ変えても名前は
snmp_result_20260923_101500.txt のままなので、

    fmt = self._export_format(file_path)   # 拡張子で決まる

が txt を返し、CSV のつもりで押した保存にテキストが書かれていた。
Syslog も同じで、さらに CSV 自体が無く txt / json しか選べなかった。

利用者の決定（2026-09-23）:
「エクスポートのファイル拡張子が txt で固定されているけど、これも csv や
log といった拡張子で保存したい」

直し方: 選んだ絞り込みの拡張子へ付け替える。ただし利用者が既定とは違う
拡張子を自分で書いたときは、その拡張子を尊重する（付け替えの判断は
core/save_defaults.py の apply_filter_suffix）。絞り込みの並びは
表や一覧のある画面で「テキスト (*.txt);;CSV (*.csv);;JSON (*.json)」に
揃え、Syslog には CSV を足す。
"""
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

TABLE_FILTERS = "テキスト (*.txt);;CSV (*.csv);;JSON (*.json)"


class _Incoming:
    """SyslogReceiver が渡してくる形に合わせた最小の受信メッセージ"""

    def __init__(self, message="link down"):
        self.timestamp = "2026-09-23 10:00:00"
        self.hostname = "rtr1"
        self.level = "Info"
        self.message = message
        self.raw_message = "<134>Sep 23 10:00:00 rtr1 " + message
        self.source_ip = "192.0.2.1"
        self.source_display = "192.0.2.1 (UDP/514)"


class ExportExtensionFollowsFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作ったウィンドウはクラス終了まで保持する（test_snmp_export_extension_case と同じ）
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-extfilter-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    # --- 道具 ------------------------------------------------------------

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-extfilter-conf-")
        self.addCleanup(shutil.rmtree, d, True)
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _run_dialog(self, module, handler, choose_ext=None, type_name=None):
        """保存ダイアログを差し替えて handler を呼び、渡された絞り込みを返す。

        choose_ext: 利用者がその種類を選んだことにする拡張子（".csv" など）
        type_name: 利用者が名前を打ち直したことにする場合のファイル名
        """
        seen = {}

        def fake(parent, title, initial, filters="", *args, **kwargs):
            seen["initial"] = initial
            seen["filters"] = filters
            name = type_name or os.path.basename(initial)
            chosen = ""
            if choose_ext is not None:
                for entry in filters.split(";;"):
                    if "*" + choose_ext in entry.lower():
                        chosen = entry
                        break
                self.assertTrue(chosen,
                                "絞り込みに %s が無い: %r" % (choose_ext, filters))
            return os.path.join(self.dir, name), chosen

        with mock.patch(module + ".QFileDialog.getSaveFileName",
                        side_effect=fake), \
                mock.patch(module + ".QMessageBox"):
            handler()
        return seen

    def _only_file(self):
        """書き出されたファイル 1 件の名前（残骸が無いことも確かめる）"""
        names = [n for n in os.listdir(self.dir) if not n.endswith(".tmp")]
        self.assertEqual(len(names), 1, "書き出されたファイル: %r" % names)
        return names[0]

    def _read(self, name):
        with io.open(os.path.join(self.dir, name), encoding="utf-8-sig") as f:
            return f.read()

    def _snmp_results_panel(self):
        panel = self._window().snmp_panel
        panel.result_model.set_results(
            [("1.3.6.1.2.1.1.5.0", "OctetString", "sw1")])
        panel._result_host = "192.0.2.10"
        return panel

    def _snmp_traps_panel(self):
        panel = self._window().snmp_panel
        panel._add_trap_to_tree({
            "source_ip": "192.0.2.20",
            "source_port": 162,
            "trap_oid": "1.3.6.1.4.1.9.9.41.2.0.1",
            "received_at": "2026-09-23T10:00:00",
            "varbinds": [{"oid": "1.3.6.1.2.1.1.5.0", "value": "sw1"}],
        })
        return panel

    def _syslog_panel(self, message="link down"):
        panel = self._window().syslog_panel
        panel.add_message(_Incoming(message))
        self.app.processEvents()
        return panel

    # --- SNMP ------------------------------------------------------------

    def test_snmp_results_csv_filter_gives_a_csv_file(self):
        """CSV を選んだら、名前も中身も CSV になること。"""
        panel = self._snmp_results_panel()
        self._run_dialog("ui.snmp_panel", panel._on_export_clicked,
                         choose_ext=".csv")
        name = self._only_file()
        self.assertTrue(name.endswith(".csv"), "拡張子が付け替わっていない: %r" % name)
        self.assertIn("OID,Type,Value", self._read(name).splitlines(),
                      "中身が CSV になっていない")

    def test_snmp_results_json_filter_gives_a_json_file(self):
        panel = self._snmp_results_panel()
        self._run_dialog("ui.snmp_panel", panel._on_export_clicked,
                         choose_ext=".json")
        name = self._only_file()
        self.assertTrue(name.endswith(".json"), "拡張子が付け替わっていない: %r" % name)
        json.loads(self._read(name))

    def test_snmp_traps_csv_filter_gives_a_csv_file(self):
        panel = self._snmp_traps_panel()
        self._run_dialog("ui.snmp_panel", panel._on_trap_export_clicked,
                         choose_ext=".csv")
        name = self._only_file()
        self.assertTrue(name.endswith(".csv"), "拡張子が付け替わっていない: %r" % name)
        self.assertTrue(self._read(name).startswith("時刻,"))

    def test_snmp_filters_are_aligned(self):
        panel = self._snmp_results_panel()
        seen = self._run_dialog("ui.snmp_panel", panel._on_export_clicked,
                                choose_ext=".txt")
        self.assertEqual(seen["filters"], TABLE_FILTERS)
        panel = self._snmp_traps_panel()
        seen = self._run_dialog("ui.snmp_panel", panel._on_trap_export_clicked,
                                choose_ext=".txt")
        self.assertEqual(seen["filters"], TABLE_FILTERS)

    # --- Syslog ----------------------------------------------------------

    def test_syslog_offers_csv(self):
        """Syslog のエクスポートに CSV があり、並びが他の画面と揃っていること。"""
        panel = self._syslog_panel()
        seen = self._run_dialog("ui.syslog_panel", panel._export_messages,
                                choose_ext=".txt")
        self.assertEqual(seen["filters"], TABLE_FILTERS)

    def test_syslog_selected_rows_stay_text_only(self):
        """選択行の保存はテキストのまま（形式を増やすのはエクスポート側だけ）。

        同じ行を書くので csv/json も選べてよさそうだが、それには書き出しの
        共通化が要り、この周の 1 コミットの上限を超える。形式を増やさない
        ことを決めとして残す（増やすなら、ここを意図して書き換える）。
        """
        panel = self._syslog_panel()
        panel.table_view.selectRow(0)
        seen = self._run_dialog("ui.syslog_panel", panel._save_selected,
                                choose_ext=".txt")
        self.assertNotIn("*.csv", seen["filters"])
        self.assertNotIn("*.json", seen["filters"])

    def test_syslog_csv_filter_gives_a_csv_file(self):
        panel = self._syslog_panel()
        self._run_dialog("ui.syslog_panel", panel._export_messages,
                         choose_ext=".csv")
        name = self._only_file()
        self.assertTrue(name.endswith(".csv"), "拡張子が付け替わっていない: %r" % name)
        with io.open(os.path.join(self.dir, name), encoding="utf-8-sig",
                     newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 2, "見出し + 1 件になっていない: %r" % rows)
        self.assertIn("link down", rows[1])
        self.assertIn("192.0.2.1 (UDP/514)", rows[1])

    def test_syslog_csv_neutralises_formulas(self):
        """受信した本文が表計算の数式として発火しないこと。"""
        panel = self._syslog_panel(message="=cmd|' /C calc'!A0")
        self._run_dialog("ui.syslog_panel", panel._export_messages,
                         choose_ext=".csv")
        with io.open(os.path.join(self.dir, self._only_file()),
                     encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertTrue(rows[1][-1].startswith("'="),
                        "数式のまま書かれている: %r" % rows[1][-1])

    def test_syslog_selected_rows_are_still_saved(self):
        """選択行の保存が、これまでどおりテキストで書けること。"""
        panel = self._syslog_panel()
        panel.table_view.selectRow(0)
        self._run_dialog("ui.syslog_panel", panel._save_selected,
                         choose_ext=".txt")
        name = self._only_file()
        self.assertTrue(name.endswith(".txt"), "拡張子が変わっている: %r" % name)
        self.assertIn("link down", self._read(name))

    # --- 付け替えの決まり ------------------------------------------------

    def test_an_extension_typed_by_the_user_is_kept(self):
        """既定と違う拡張子を自分で書いたら、選んだ種類より優先すること。"""
        panel = self._snmp_results_panel()
        self._run_dialog("ui.snmp_panel", panel._on_export_clicked,
                         choose_ext=".txt", type_name="out.json")
        name = self._only_file()
        self.assertEqual(name, "out.json", "自分で書いた拡張子が消えた: %r" % name)
        json.loads(self._read(name))

    def test_a_name_without_an_extension_gets_the_chosen_one(self):
        panel = self._snmp_results_panel()
        self._run_dialog("ui.snmp_panel", panel._on_export_clicked,
                         choose_ext=".csv", type_name="out")
        self.assertEqual(self._only_file(), "out.csv")

    def test_an_uppercase_extension_is_not_doubled(self):
        """out.CSV に CSV を選んでも out.CSV.csv にしないこと。"""
        panel = self._snmp_results_panel()
        self._run_dialog("ui.snmp_panel", panel._on_export_clicked,
                         choose_ext=".csv", type_name="out.CSV")
        self.assertEqual(self._only_file(), "out.CSV")

    # --- 端末 ------------------------------------------------------------

    def test_terminal_log_save_follows_the_filter(self):
        """全ログ保存で「テキスト」を選んだら .txt になること。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        w.append_output("rtrA", "Router#show version\r\n")

        seen = {}

        def fake(parent, title, initial, filters="", *args, **kwargs):
            seen["filters"] = filters
            entry = [e for e in filters.split(";;") if "*.txt" in e][0]
            return os.path.join(self.dir, os.path.basename(initial)), entry

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        side_effect=fake), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog"
                           ) as progress, \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            progress.return_value.exec.return_value = True
            w.save_current_log()
        # 実際に書くのは進捗ダイアログなので、渡された保存先を見る
        target = progress.call_args[0][1]
        self.assertIn("*.txt", seen["filters"])
        self.assertTrue(target.endswith(".txt"),
                        "拡張子が付け替わっていない: %r" % target)

    def test_terminal_wildcard_filter_leaves_the_name_alone(self):
        """「すべてのファイル (*.*)」では名前に触らないこと。"""
        from core import save_defaults
        self.assertEqual(
            save_defaults.apply_filter_suffix("a.log", "すべてのファイル (*.*)",
                                              "a.log"),
            "a.log")


if __name__ == "__main__":
    unittest.main()
