"""記録中のログファイルを、別の記録や全ログ保存の保存先にできないことを検証する。

タブ A が記録中のファイルを、タブ B の「ログ記録開始」や「全ログ保存」の
保存先に選ぶと、open('w') で A の内容が消え、以降は A のオフセットからの
書き込みと B の書き込みが NUL 埋めの穴を挟んで混在した（実測: final file
= b'B1 show ver\\r\\n' + NUL x18 + b'A3 shutdown\\r\\n...'）。全ログ保存でも
保存済み内容の上へ A の記録が上書きされた。

記録中のファイル（abspath で比較）が選ばれたら拒否して警告し、
既存の記録には触れない。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class LogFileInUseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-loginuse-")
        self._old_cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._old_cwd)
        info = mock.patch("PyQt6.QtWidgets.QMessageBox.information")
        info.start()
        self.addCleanup(info.stop)
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.addCleanup(mock.patch.stopall)
        self.target = os.path.join(self.dir, "shared.log")

    def _widget_recording_a(self):
        """rtrA と rtrB のタブを作り、rtrA を self.target へ記録中にする。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        w.create_terminal_tab("rtrB")
        w.tab_widget.setCurrentIndex(w.tab_widget.indexOf(w._terminals["rtrA"]))
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")):
            w.start_log_recording()
        self.assertIn("rtrA", w._log_files, "前提: rtrA の記録が始まっている")
        w.append_output("rtrA", "A1 conf t\r\n")
        w.append_output("rtrA", "A2 interface Gi0/1\r\n")
        self.addCleanup(lambda: w.stop_log_recording("rtrA"))
        return w

    def _read_target(self):
        with open(self.target, "rb") as f:
            return f.read()

    def test_recording_start_refuses_a_file_another_tab_is_recording_to(self):
        w = self._widget_recording_a()
        before = self._read_target()
        self.assertIn(b"A1 conf t", before, "前提: rtrA の記録が書けている")

        w.tab_widget.setCurrentIndex(w.tab_widget.indexOf(w._terminals["rtrB"]))
        # 相対パスで同じファイルを選んでも同一と見なすこと
        chosen = os.path.relpath(self.target, self.dir)
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(chosen, "")):
            w.start_log_recording()

        self.assertNotIn("rtrB", w._log_files, "使用中のファイルへ記録を始めてしまった")
        self.assertIn("rtrA", w._log_files, "rtrA の記録が止まっている")
        self.assertEqual(self.warning.call_count, 1, "利用者に拒否を知らせていない")
        self.assertEqual(self._read_target(), before, "既存の記録が壊された")

        w.append_output("rtrA", "A3 shutdown\r\n")
        self.assertEqual(self._read_target(), before + b"A3 shutdown\r\n",
                         "拒否後の rtrA の記録が壊れている")

    def test_full_log_save_refuses_a_file_being_recorded_to(self):
        w = self._widget_recording_a()
        before = self._read_target()

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog") as dlg:
            w.save_current_log()

        self.assertEqual(dlg.call_count, 0, "使用中のファイルへ保存を始めてしまった")
        self.assertIn("rtrA", w._log_files, "rtrA の記録が止まっている")
        self.assertEqual(self.warning.call_count, 1, "利用者に拒否を知らせていない")
        self.assertEqual(self._read_target(), before, "既存の記録が壊された")

    def test_recording_start_still_accepts_a_different_file(self):
        w = self._widget_recording_a()
        other = os.path.join(self.dir, "other.log")
        w.tab_widget.setCurrentIndex(w.tab_widget.indexOf(w._terminals["rtrB"]))
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(other, "")):
            w.start_log_recording()
        self.addCleanup(lambda: w.stop_log_recording("rtrB"))
        self.assertIn("rtrB", w._log_files, "別ファイルなのに記録が始まらない")
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
