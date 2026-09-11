"""エクスポートに書く「対象ホスト」が、結果を取得した接続先であることを検証する。

_export_results_to_json / _export_results_to_txt は、保存する時点の
host_edit.text() を対象ホストとして書いていた。A から取得したあと入力欄だけ
B に変えて保存すると、A の結果が B の記録として残る（実測で再現）。B への
要求が失敗しても結果表は消えないので、同じ取り違えが起きる。

要求を出した時点のホストを結果と一緒に持ち、書き出しはそれを使う。
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
    ("1.3.6.1.2.1.1.5.0", "OctetString", "router-a"),
]


class SnmpExportHostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        panel = window.snmp_panel
        # 実際の通信はしない。要求が出たことだけ見る
        panel.snmp_manager.snmp_walk = mock.Mock()
        panel.oid_edit.setText("1.3.6.1.2.1.1")
        return panel

    def _walk_from(self, panel, host):
        panel.host_edit.setText(host)
        with mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_walk_clicked()
        panel.snmp_manager.snmp_walk.assert_called()

    def _export(self, panel, kind):
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-snmp-host-"),
                            "out." + kind)
        getattr(panel, "_export_results_to_" + kind)(
            path, panel.result_model.get_all_results(),
            panel._result_host, panel._last_partial_reason)
        with io.open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_exported_host_is_the_one_the_results_came_from(self):
        panel = self._panel()
        self._walk_from(panel, "192.0.2.10")
        panel._on_operation_completed(True, list(ROWS))

        panel.host_edit.setText("192.0.2.99")   # 保存前に入力欄だけ変える

        data = json.loads(self._export(panel, "json"))
        self.assertEqual(data["host"], "192.0.2.10",
                         "保存時の入力欄の値を書いている")
        self.assertIn("対象ホスト: 192.0.2.10", self._export(panel, "txt"))

    def test_a_failed_request_to_another_host_does_not_relabel_old_results(self):
        panel = self._panel()
        self._walk_from(panel, "192.0.2.10")
        panel._on_operation_completed(True, list(ROWS))

        self._walk_from(panel, "192.0.2.99")
        with mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_operation_completed(False, "timeout")

        data = json.loads(self._export(panel, "json"))
        self.assertEqual(data["host"], "192.0.2.10",
                         "失敗した要求先のホスト名で古い結果を書いている")

    def test_a_request_refused_as_busy_does_not_change_the_recorded_host(self):
        """実行中に別ホストで押しても、受理されなかった要求のホストは記録しないこと。

        マネージャは worker 実行中の要求を「既に操作が実行中です」で断り、
        開始しない。その要求のホストを記録すると、A の結果に B のホストが
        付いて保存される。
        """
        panel = self._panel()
        self._walk_from(panel, "192.0.2.10")          # A の WALK が走り出す

        # A が走っている間に B で押す → マネージャは断って開始しない
        panel.snmp_manager.is_busy = mock.Mock(return_value=True)
        panel.snmp_manager.snmp_walk = mock.Mock(
            side_effect=lambda *a, **k: panel.snmp_manager.error_occurred.emit("既に操作が実行中です"))
        panel.host_edit.setText("192.0.2.99")
        with mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_walk_clicked()

        panel._on_operation_completed(True, list(ROWS))   # A の結果が届く

        data = json.loads(self._export(panel, "json"))
        self.assertEqual(data["host"], "192.0.2.10",
                         "断られた要求（B）のホストを A の結果に付けている")

    def test_a_successful_request_to_another_host_relabels(self):
        """対照: 別ホストで取り直せば、そのホストになる。"""
        panel = self._panel()
        self._walk_from(panel, "192.0.2.10")
        panel._on_operation_completed(True, list(ROWS))
        self._walk_from(panel, "192.0.2.99")
        panel._on_operation_completed(True, list(ROWS))

        data = json.loads(self._export(panel, "json"))
        self.assertEqual(data["host"], "192.0.2.99")


if __name__ == "__main__":
    unittest.main()
