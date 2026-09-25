"""SNMP のエクスポートが、端末が記録中のログファイルを壊さないことを検証する。

端末どうしの上書きは tests/test_log_file_in_use_refused.py で塞いだが、
その検査は TerminalWidget の中にあり、SNMP パネルは通らない。
SNMP パネルの6つの書き出しは保存先を丸ごと作り直すので、記録中の
ファイルを選ぶと記録済みの内容が失われ、端末は開いたままのハンドルで
自分のオフセットから書き続ける（実測: 双方のファイルが壊れる）。

記録中のファイルが選ばれたら、端末の場合と同じく拒否して警告する。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SnmpExportRespectsRecordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-snmprec-")
        self._old_cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._old_cwd)
        self.info = mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.critical = mock.patch("PyQt6.QtWidgets.QMessageBox.critical").start()
        self.addCleanup(mock.patch.stopall)
        self.target = os.path.join(self.dir, "rtrA.log")

    def _recording_terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        w.tab_widget.setCurrentIndex(w.tab_widget.indexOf(w._terminals["rtrA"]))
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")):
            w.start_log_recording()
        self.assertIn("rtrA", w._log_files, "前提: 記録が始まっている")
        w.append_output("rtrA", "A1 show running-config\r\n")
        self.addCleanup(lambda: w._stop_log_recording_for("rtrA"))
        # 記録開始の「開始しました」通知を、エクスポートの結果と混ぜない
        self.info.reset_mock()
        self.warning.reset_mock()
        return w

    def _panel_with_one_result(self):
        from ui.snmp_panel import SNMPPanel
        panel = SNMPPanel()
        self.addCleanup(panel.close)
        # 結果行は (OID, Type, Value) の組。SNMPWorker が渡す形に合わせる。
        # dict を置くと書き出しが KeyError で落ち、「普通の保存はできる」側の
        # 検査が保存の成否を見られない
        panel.result_model.set_results([
            ("1.3.6.1.2.1.1.5.0", "OctetString", "rtrB"),
        ])
        panel._result_host = "192.0.2.1"
        return panel

    def _panel_with_one_trap(self):
        panel = self._panel_with_one_result()
        panel.trap_data_list = [{
            "timestamp": "2026-01-01 00:00:00",
            "source": "192.0.2.9",
            "community": "public",
            "varbinds": [{"oid": "1.3.6.1.2.1.1.3.0", "value": "42"}],
        }]
        return panel

    def _target_bytes(self):
        with io.open(self.target, "rb") as f:
            return f.read()

    def test_exporting_results_over_a_recording_log_is_refused(self):
        self._recording_terminal()
        before = self._target_bytes()
        self.assertIn(b"show running-config", before, "前提: 記録が書かれている")
        panel = self._panel_with_one_result()

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")):
            panel._on_export_clicked()

        self.assertEqual(self._target_bytes(), before,
                         "記録中のログを SNMP のエクスポートが上書きした")
        self.assertTrue(self.warning.called, "使用中だと知らせていない")
        self.assertFalse(self.info.called, "成功として扱っている")

    def test_exporting_traps_over_a_recording_log_is_refused(self):
        self._recording_terminal()
        before = self._target_bytes()
        panel = self._panel_with_one_trap()

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")):
            panel._on_trap_export_clicked()

        self.assertEqual(self._target_bytes(), before,
                         "記録中のログを Trap のエクスポートが上書きした")
        self.assertTrue(self.warning.called, "使用中だと知らせていない")
        self.assertFalse(self.info.called, "成功として扱っている")

    def test_exporting_to_a_free_path_still_works(self):
        self._recording_terminal()
        panel = self._panel_with_one_result()
        other = os.path.join(self.dir, "export.txt")

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(other, "")):
            panel._on_export_clicked()

        self.assertTrue(os.path.exists(other), "普通の保存まで拒否している")
        self.assertFalse(self.warning.called)


if __name__ == "__main__":
    unittest.main()
