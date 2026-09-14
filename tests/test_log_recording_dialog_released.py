"""記録中ダイアログが、記録を止めたあとも子として残り続けないことを検証する。

LogRecordingDialog は TerminalWidget を親にして show() しているので、
close() しても Qt の親子関係からは外れない。記録を開始して停止するたびに
TerminalWidget の子が 1 件ずつ増え、1 秒ごとのタイマーを持つダイアログが
そのまま積み上がる（実測: 開始/停止 3 回で LogRecordingDialog が 3 件）。

止めたダイアログは破棄されて、繰り返しても増えないこと。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class LogRecordingDialogReleasedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.tmpdir = tempfile.mkdtemp(prefix="netbelt-logdlg-")

    def _pump(self):
        """遅延削除まで含めて、溜まったイベントを捌く。"""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtWidgets import QApplication
        for _ in range(3):
            QApplication.processEvents()
            QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _record_once(self, widget, index):
        path = os.path.join(self.tmpdir, "rtrA-%d.log" % index)
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
             mock.patch("PyQt6.QtWidgets.QMessageBox.warning"), \
             mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            widget.start_log_recording()
            self.assertIn("rtrA", widget._log_files, "前提: 記録が始まっている")
            widget.stop_log_recording("rtrA")
        self.assertNotIn("rtrA", widget._log_files, "前提: 記録が止まっている")
        self._pump()

    def test_stopped_dialogs_do_not_pile_up_on_the_terminal(self):
        from ui.dialogs.log_recording_dialog import LogRecordingDialog
        from ui.terminal_widget import TerminalWidget

        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        before = len(w.findChildren(LogRecordingDialog))
        self.assertEqual(before, 0, "前提: まだダイアログは無い")

        for index in range(3):
            self._record_once(w, index)

        after = len(w.findChildren(LogRecordingDialog))
        self.assertEqual(after, 0,
                         "止めたダイアログが残っている: %d 件" % after)


if __name__ == "__main__":
    unittest.main()
