"""保存先が他のプログラムに開かれている時、原因が分かる文を出すことを検証する。

SNMP の書き出しは一時ファイルへ書き切ってから os.replace() で保存先を
置き換える（atomic_text_write）。Windows では保存先を他のハンドルが開いた
ままだと、この置き換えが PermissionError になる。

実測（ビューアに見立てて read で開いたまま同じパスへ書き出した）:
    atomic_text_write while held open: FAILED PermissionError(13, ...)

元の内容は残り一時ファイルも消えるので壊れはしないが、出る文が
「エクスポート中にエラーが発生しました: [Errno 13] アクセスが拒否されました」
だけでは、開いているファイルを閉じれば通ると利用者に分からない。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SnmpExportFileInUseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-snmpinuse-")
        self.target = os.path.join(self.dir, "export.txt")
        with io.open(self.target, "w", encoding="utf-8") as f:
            f.write("これは上書き前からあった内容\n")
        self.info = mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.critical = mock.patch("PyQt6.QtWidgets.QMessageBox.critical").start()
        self.addCleanup(mock.patch.stopall)

    def _panel(self):
        from ui.snmp_panel import SNMPPanel
        panel = SNMPPanel()
        self.addCleanup(panel.close)
        panel.result_model.set_results([
            ("1.3.6.1.2.1.1.5.0", "OctetString", "rtrB"),
        ])
        panel._result_host = "192.0.2.1"
        panel.trap_data_list = [{
            "timestamp": "2026-01-01 00:00:00",
            "source_ip": "192.0.2.9",
            "source_port": 162,
            "trap_oid": "1.3.6.1.6.3.1.1.5.3",
            "varbinds": [{"oid": "1.3.6.1.2.1.1.3.0", "value": "42"}],
        }]
        return panel

    def _hold_target_open(self):
        """ビューアが開いたままの状態を作る。"""
        holder = io.open(self.target, "r", encoding="utf-8")
        self.addCleanup(holder.close)
        return holder

    def _export(self, panel, method_name):
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")):
            getattr(panel, method_name)()

    def _critical_text(self):
        self.assertTrue(self.critical.called, "失敗を知らせていない")
        return self.critical.call_args[0][2]

    def test_results_export_to_a_held_file_says_the_file_is_in_use(self):
        self._hold_target_open()
        panel = self._panel()

        self._export(panel, "_on_export_clicked")

        text = self._critical_text()
        self.assertIn("別のプログラム", text,
                      "使用中が原因だと分かる文になっていない: " + text)
        self.assertIn(self.target, text, "どのファイルか出ていない")
        self.assertFalse(self.info.called, "成功として扱っている")

    def test_trap_export_to_a_held_file_says_the_file_is_in_use(self):
        self._hold_target_open()
        panel = self._panel()

        self._export(panel, "_on_trap_export_clicked")

        text = self._critical_text()
        self.assertIn("別のプログラム", text,
                      "使用中が原因だと分かる文になっていない: " + text)
        self.assertIn(self.target, text, "どのファイルか出ていない")
        self.assertFalse(self.info.called, "成功として扱っている")

    def test_other_failures_still_show_the_plain_message(self):
        """対照: 使用中以外の失敗は、これまでどおりの文のまま。"""
        panel = self._panel()
        with mock.patch.object(panel, "_export_results_to_txt",
                               side_effect=RuntimeError("disk full")):
            self._export(panel, "_on_export_clicked")

        text = self._critical_text()
        self.assertIn("エクスポート中にエラーが発生しました", text)
        self.assertIn("disk full", text)
        self.assertNotIn("別のプログラム", text)

    def test_exporting_to_a_free_path_still_works(self):
        """対照: 開かれていない保存先なら、これまでどおり成功する。"""
        panel = self._panel()
        self._export(panel, "_on_export_clicked")

        self.assertFalse(self.critical.called, "普通の保存が失敗している")
        self.assertTrue(self.info.called, "成功を知らせていない")
        with io.open(self.target, encoding="utf-8") as f:
            self.assertIn("192.0.2.1", f.read())


if __name__ == "__main__":
    unittest.main()
