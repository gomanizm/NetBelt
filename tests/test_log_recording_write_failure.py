"""ログ記録の書き込みに失敗したら、記録を止めて知らせることを検証する。

append_output のログ書き込みは、例外を stderr への print で握っていた。
ディスク満杯・共有フォルダの切断・権限の消失が起きても、記録フラグ・
「記録中」ダイアログ・右クリックメニューはそのままで、警告も出ない。
利用者は記録できていると思って設定作業を続け、証跡は失敗前の行で
終わっている（実測: OSError 注入後も記録中表示のまま、警告 0 回）。
console=False の exe では stderr の出力先も無い。

失敗したら、その機器の記録を止めて後始末し、一度だけ警告を出す。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _FailingHandle:
    """write() で「ディスク満杯」を起こすログファイルの代役。"""

    def __init__(self):
        self.closed = False
        self.attempts = 0

    def write(self, text):
        self.attempts += 1
        raise OSError(28, "No space left on device")

    def flush(self):
        pass

    def close(self):
        self.closed = True


class LogRecordingWriteFailureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _recording_widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-logfail-"), "rtrA.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
             mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            w.start_log_recording()
        self.assertIn("rtrA", w._log_files, "前提: 記録が始まっている")
        w.append_output("rtrA", "line-before-failure\n")
        real = w._log_files["rtrA"]
        real.close()
        handle = _FailingHandle()
        w._log_files["rtrA"] = handle
        return w, handle

    def test_a_write_failure_stops_the_recording(self):
        w, handle = self._recording_widget()
        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning"):
            w.append_output("rtrA", "after-failure\n")

        self.assertNotIn("rtrA", w._log_files, "失敗後も記録中のままになっている")
        self.assertFalse(w._terminals["rtrA"]._is_recording)
        self.assertNotIn("rtrA", w._log_dialogs, "記録中ダイアログが残っている")
        self.assertTrue(handle.closed, "壊れたハンドルを閉じていない")

    def test_the_user_is_warned_once(self):
        w, handle = self._recording_widget()
        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning") as warning:
            w.append_output("rtrA", "after-failure\n")
            w.append_output("rtrA", "and-more\n")

        self.assertEqual(warning.call_count, 1,
                         "警告が出ていない、または連発している: %d" % warning.call_count)
        shown = " ".join(str(a) for a in warning.call_args[0])
        self.assertIn("rtrA", shown)

    def test_output_keeps_flowing_to_the_screen_after_the_failure(self):
        """記録の失敗で表示まで止めないこと。"""
        w, handle = self._recording_widget()
        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning"):
            w.append_output("rtrA", "after-failure\n")
            w.append_output("rtrA", "still-visible\n")
        self.assertIn("still-visible", w._terminals["rtrA"].toPlainText())


if __name__ == "__main__":
    unittest.main()
