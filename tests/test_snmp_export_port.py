"""GET/WALK の書き出しに、結果を取得したポートが残ることを検証する。

何が起きていたか（実測、基準 441ea02）: 127.0.0.1 の別ポートに偽エージェントを
2 つ（sysName=agent-A / agent-B）立て、パネルの WALK と GET から本物の
SNMPWorker で取得して CSV/JSON/TXT に書き出すと、どの形式にもポート番号が
出なかった。JSON のキーは host だけで A も B も "127.0.0.1"、CSV は
「# 対象ホスト: 127.0.0.1」、TXT は「対象ホスト: 127.0.0.1」だけ。同じホストの
別ポート（NAT の先の機器、161 以外で待つエージェント）から採った結果は、
ファイルを並べても値以外で区別できなかった。パネルはポートをワーカーへ
渡すだけで、どこにも残していなかった。

利用者の決定（snmp-02 (a)）: ポートを別の欄で足す。JSON は "port": 161、CSV は
「# 対象ポート: 161」の行、TXT は「対象ポート: 161」の行。host の値は今のまま。
既定の 161 でも書く。

どう直したか: 要求が受理されたときにホストと一緒にポートを固定し
（_request_port）、結果（完了・取り消し）が届いたときに _result_port へ移す。
書き出しはホストと同じくダイアログを開く前に _result_port を固定して渡す。
保存時のポート欄の値は使わない（A の結果が B のポートの記録になる）。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

HOST = "192.0.2.10"
ROWS = [("1.3.6.1.2.1.1.5.0", "OctetString", "router-a")]


class SnmpExportPortTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.snmp_panel import SNMPPanel
        # MIB の読み込みはこの検査と関係が無いので走らせない
        with mock.patch.object(SNMPPanel, "_start_background_mib_loading"):
            self.panel = SNMPPanel()
        self.addCleanup(self.panel.deleteLater)
        # 実際の通信はしない。要求は受理されたことにする
        self.panel.snmp_manager = mock.Mock()
        self.panel.snmp_manager.snmp_get.return_value = True
        self.panel.snmp_manager.snmp_walk.return_value = True
        self.panel.host_edit.setText(HOST)
        self.out_dir = tempfile.mkdtemp(prefix="netbelt-snmp-port-")
        self.addCleanup(shutil.rmtree, self.out_dir, True)

    def _request_at(self, port, op="walk"):
        """ポート欄を port にして GET/WALK を押す（結果はまだ届かない）。"""
        self.panel.port_spinbox.setValue(port)
        self.panel.oid_edit.setText(
            "1.3.6.1.2.1.1" if op == "walk" else "1.3.6.1.2.1.1.5.0")
        with mock.patch("ui.snmp_panel.QMessageBox"):
            getattr(self.panel, "_on_%s_clicked" % op)()

    def _fetch_at(self, port, op="walk"):
        self._request_at(port, op)
        self.panel._on_operation_completed(True, list(ROWS))

    def _export(self, kind, during_dialog=None):
        """画面のエクスポートと同じ経路で書き出し、中身を返す。"""
        path = os.path.join(self.out_dir, "out." + kind)

        def dialog(*args, **kwargs):
            if during_dialog:
                during_dialog()
            return path, ""

        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        side_effect=dialog), \
                mock.patch("ui.snmp_panel.QMessageBox"):
            self.panel._on_export_clicked()
        with io.open(path, encoding="utf-8-sig", newline="") as f:
            return f.read()

    def _ports_in_all_formats(self):
        data = json.loads(self._export("json"))
        csv_lines = self._export("csv").splitlines()
        txt_lines = self._export("txt").splitlines()
        return data, csv_lines, txt_lines

    def test_each_format_records_the_port_the_results_came_from(self):
        for op in ("walk", "get"):
            with self.subTest(op=op):
                self._fetch_at(1161, op)
                # 保存前にポート欄だけ変える。書き出しは取得したポートのまま
                self.panel.port_spinbox.setValue(2161)
                data, csv_lines, txt_lines = self._ports_in_all_formats()
                self.assertEqual(data.get("port"), 1161,
                                 "JSON に取得したポートが無い")
                self.assertIn("# 対象ポート: 1161", csv_lines)
                self.assertIn("対象ポート: 1161", txt_lines)

    def test_the_host_value_is_unchanged(self):
        """ポートは別の欄。host を「host:port」にまとめない。"""
        self._fetch_at(1161)
        data, csv_lines, txt_lines = self._ports_in_all_formats()
        self.assertEqual(data["host"], HOST)
        self.assertIn("# 対象ホスト: %s" % HOST, csv_lines)
        self.assertIn("対象ホスト: %s" % HOST, txt_lines)

    def test_the_port_line_follows_the_host_line(self):
        """CSV は見出しの前、TXT は対象ホストの次の行に置く。"""
        self._fetch_at(1161)
        _data, csv_lines, txt_lines = self._ports_in_all_formats()
        self.assertEqual(csv_lines[:3], ["# 対象ホスト: %s" % HOST,
                                         "# 対象ポート: 1161",
                                         "OID,Type,Value"])
        host_at = txt_lines.index("対象ホスト: %s" % HOST)
        self.assertEqual(txt_lines[host_at + 1], "対象ポート: 1161")

    def test_the_default_port_is_written_too(self):
        self._fetch_at(161)
        data, csv_lines, txt_lines = self._ports_in_all_formats()
        self.assertEqual(data.get("port"), 161)
        self.assertIn("# 対象ポート: 161", csv_lines)
        self.assertIn("対象ポート: 161", txt_lines)

    def test_two_ports_on_the_same_host_are_told_apart(self):
        self._fetch_at(1161)
        first = json.loads(self._export("json"))
        self._fetch_at(2161)
        second = json.loads(self._export("json"))
        self.assertEqual((first["host"], second["host"]), (HOST, HOST))
        self.assertEqual((first.get("port"), second.get("port")),
                         (1161, 2161),
                         "同じホストの別ポートの結果を区別できない")

    def test_the_port_is_fixed_before_the_save_dialog(self):
        """ダイアログの間に別ポートの WALK が完走しても、押した時点の結果のポート。

        モーダルダイアログはネストしたイベントループで queued シグナルを
        処理するので、開いている間に次の結果が届く（ホストと同じ理由）。
        """
        self._fetch_at(1161)
        data = json.loads(self._export(
            "json", during_dialog=lambda: self._fetch_at(2161)))
        self.assertEqual(data.get("port"), 1161)

    def test_a_refused_request_does_not_change_the_recorded_port(self):
        """実行中で断られた要求のポートは記録しない。"""
        self._fetch_at(1161)
        self.panel.snmp_manager.snmp_walk.return_value = False
        self._request_at(2161)
        self.assertEqual(json.loads(self._export("json")).get("port"), 1161)

    def test_a_failed_request_keeps_the_port_of_the_results_shown(self):
        """失敗した要求では表の結果は変わらないので、ポートも変えない。"""
        self._fetch_at(1161)
        self._request_at(2161)
        with mock.patch("ui.snmp_panel.QMessageBox"):
            self.panel._on_operation_completed(False, "timeout")
        self.assertEqual(json.loads(self._export("json")).get("port"), 1161)

    def test_the_result_of_the_request_in_flight_gets_its_own_port(self):
        """要求の後にポート欄を変えても、届いた結果は要求したポートのもの。"""
        self._request_at(1161)
        self.panel.port_spinbox.setValue(2161)
        self.panel._on_operation_completed(True, list(ROWS))
        self.assertEqual(json.loads(self._export("json")).get("port"), 1161)

    def test_a_stopped_walk_records_its_port(self):
        """利用者が止めた WALK の途中までの行にも、そのポートを付ける。"""
        self._fetch_at(1161)
        self._request_at(2161)
        self.panel._on_operation_cancelled(list(ROWS))
        data = json.loads(self._export("json"))
        self.assertEqual(data.get("port"), 2161)
        self.assertFalse(data["complete"])

    def test_the_writers_keep_their_old_output_without_a_port(self):
        """対照: ポートを渡さずに呼んだ書き出しは、従来と同じ中身のまま。"""
        path = os.path.join(self.out_dir, "direct.json")
        self.panel._export_results_to_json(path, list(ROWS), HOST, None)
        with io.open(path, encoding="utf-8") as f:
            self.assertNotIn("port", json.load(f))
        path = os.path.join(self.out_dir, "direct.csv")
        self.panel._export_results_to_csv(path, list(ROWS), HOST, None)
        with io.open(path, encoding="utf-8-sig") as f:
            self.assertNotIn("対象ポート", f.read())
        path = os.path.join(self.out_dir, "direct.txt")
        self.panel._export_results_to_txt(path, list(ROWS), HOST, None)
        with io.open(path, encoding="utf-8") as f:
            self.assertNotIn("対象ポート", f.read())


if __name__ == "__main__":
    unittest.main()
