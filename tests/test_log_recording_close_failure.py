"""ログファイルの close() が失敗しても、記録状態を残さないことを検証する。

_stop_log_recording_for() は close() が成功した後にだけ後始末（ハンドルの
削除・記録フラグの解除・記録中ダイアログの片付け・log_recording からの
登録解除）をしていた。close() が例外を投げると警告だけが出て記録状態が
そのまま残り、次に「ログ記録開始」を選んでも「既にログ記録中です。」で
拒否される。SNMP のエクスポートも、そのパスを記録中として断り続ける。

close() が失敗しても、記録は止まって後始末が済み、記録を始め直せること。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _CloseFailingHandle:
    """close() で失敗するログファイルの代役。"""

    def __init__(self):
        self.close_attempts = 0

    def write(self, text):
        pass

    def flush(self):
        pass

    def close(self):
        self.close_attempts += 1
        raise OSError(5, "Input/output error")


class LogRecordingCloseFailureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.tmpdir = tempfile.mkdtemp(prefix="netbelt-logclose-")

    def _start_recording(self, widget, filename):
        """記録を開始し、（パス, information の mock）を返す。"""
        path = os.path.join(self.tmpdir, filename)
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
             mock.patch("PyQt6.QtWidgets.QMessageBox.warning"), \
             mock.patch("PyQt6.QtWidgets.QMessageBox.information") as info:
            widget.start_log_recording()
        return path, info

    def _stop_recording(self, widget):
        """警告・通知を止めた状態で記録を停止する（モーダルで固まらせない）。"""
        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning") as warning, \
             mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            widget.stop_log_recording("rtrA")
        return warning

    def _recording_widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        # 壊れたハンドルを残したまま片付けに入らせない
        self.addCleanup(lambda: w._log_files.clear())
        w.create_terminal_tab("rtrA")
        path, _ = self._start_recording(w, "rtrA.log")
        self.assertIn("rtrA", w._log_files, "前提: 記録が始まっている")
        w._log_files["rtrA"].close()
        handle = _CloseFailingHandle()
        w._log_files["rtrA"] = handle
        return w, handle, path

    def test_close_failure_clears_the_recording_state(self):
        w, handle, path = self._recording_widget()
        self._stop_recording(w)

        self.assertEqual(handle.close_attempts, 1, "close() を試していない")
        self.assertNotIn("rtrA", w._log_files, "失敗後も記録中のままになっている")
        self.assertFalse(w._terminals["rtrA"]._is_recording,
                         "記録フラグが残っている")
        self.assertNotIn("rtrA", w._log_dialogs, "記録中ダイアログが残っている")

        from core import log_recording
        self.assertIsNone(log_recording.device_using(path),
                          "記録先の登録が残っている")

    def test_the_user_is_warned_about_the_close_failure(self):
        w, handle, path = self._recording_widget()
        warning = self._stop_recording(w)

        self.assertEqual(warning.call_count, 1,
                         "警告の回数が想定と違う: %d" % warning.call_count)
        shown = " ".join(str(a) for a in warning.call_args[0])
        self.assertIn("Input/output error", shown)

    def test_recording_can_be_started_again_after_a_close_failure(self):
        w, handle, path = self._recording_widget()
        self._stop_recording(w)

        new_path, info = self._start_recording(w, "rtrA-2.log")
        shown = " ".join(str(a) for c in info.call_args_list for a in c[0])
        self.assertNotIn("既にログ記録中です。", shown,
                         "記録を始め直せない: %s" % shown)
        self.assertIsNot(w._log_files.get("rtrA"), handle,
                         "閉じられなかったハンドルが居座っている")

        self._stop_recording(w)
        self.assertTrue(os.path.exists(new_path), "新しい記録先が作られていない")


if __name__ == "__main__":
    unittest.main()
