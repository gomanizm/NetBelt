"""Syslog パネルの保存が、端末が記録中のログファイルを壊さないことを検証する。

記録中のファイルかどうかの判定は core/log_recording.py に集めてあり、
SNMP パネルは _refuse_if_recording から呼んでいる（tests/
test_snmp_export_respects_recording.py）。Syslog パネルの 2 つの保存経路
（_export_messages と _save_selected）はそれを見ていないので、端末が記録中
のログを保存先に選ばれても警告せず置き換えを試みる。

Windows では端末が開いたままのハンドルのおかげで os.replace が
PermissionError で弾かれ記録自体は残るが、利用者に出るのは「使用中です」
ではなく汎用の保存失敗ダイアログになる。ハンドルの振る舞いに頼らず、
SNMP と同じく先に断って知らせる。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

RECORDED = "A1 show running-config\r\n"


class _Incoming:
    """受信側 SyslogMessage の最小形（panel.add_message が読む属性だけ）。"""

    def __init__(self, text):
        self.timestamp = "2026-01-01 00:00:00"
        self.hostname = "sw1"
        self.level = "Info"
        self.message = text
        self.raw_message = "<134>Jan  1 00:00:00 sw1 " + text
        self.source_ip = "192.0.2.5"
        self.source_display = "192.0.2.5 (UDP/514)"


class SyslogExportRespectsRecordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        from ui.syslog_panel import SyslogPanel

        self.dir = tempfile.mkdtemp(prefix="netbelt-syslogrec-")
        self.target = os.path.join(self.dir, "rtrA.log")
        with io.open(self.target, "w", encoding="utf-8", newline="") as f:
            f.write(RECORDED)
        log_recording.start("rtrA", self.target)
        self.addCleanup(log_recording.stop, "rtrA")

        self.panel = SyslogPanel()
        self.addCleanup(self.panel.close)
        for text in ("link down", "link up"):
            self.panel.add_message(_Incoming(text))
        self.app.processEvents()

        self.info = mock.patch("ui.syslog_panel.QMessageBox.information").start()
        self.warning = mock.patch("ui.syslog_panel.QMessageBox.warning").start()
        self.critical = mock.patch("ui.syslog_panel.QMessageBox.critical").start()
        self.addCleanup(mock.patch.stopall)

    def _run(self, method, filename):
        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=(filename, "")):
            method()

    def _target_bytes(self):
        with io.open(self.target, "rb") as f:
            return f.read()

    def _assert_refused(self):
        self.assertEqual(self._target_bytes(), RECORDED.encode("utf-8"),
                         "記録中のログが書き換えられた")
        self.assertTrue(self.warning.called, "使用中だと知らせていない")
        self.assertFalse(self.info.called, "成功として扱っている")
        self.assertFalse(self.critical.called,
                         "使用中ではなく汎用の保存失敗として扱っている")

    def test_exporting_over_a_recording_log_is_refused(self):
        self._run(self.panel._export_messages, self.target)
        self._assert_refused()

    def test_saving_selected_over_a_recording_log_is_refused(self):
        self.panel.table_view.selectRow(0)
        self.warning.reset_mock()
        self._run(self.panel._save_selected, self.target)
        self._assert_refused()

    def test_exporting_to_a_free_path_still_works(self):
        other = os.path.join(self.dir, "export.txt")
        self._run(self.panel._export_messages, other)

        self.assertTrue(os.path.exists(other), "普通の保存まで拒否している")
        self.assertFalse(self.warning.called)
        self.assertTrue(self.info.called)

    def test_saving_selected_to_a_free_path_still_works(self):
        self.panel.table_view.selectRow(0)
        other = os.path.join(self.dir, "selected.txt")
        self._run(self.panel._save_selected, other)

        self.assertTrue(os.path.exists(other), "普通の保存まで拒否している")
        self.assertFalse(self.warning.called)
        self.assertTrue(self.info.called)


if __name__ == "__main__":
    unittest.main()
