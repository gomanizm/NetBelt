"""作業ディレクトリに logs を作れなくても、保存先を選べることを検証する。

全ログ保存とログ記録開始は、ファイル選択ダイアログを出す前に
cwd/logs を makedirs していた。cwd に書けない（読み取り専用の場所から
起動した、logs という名前のファイルが既にある）と、ここで例外になり
ダイアログは一度も出ない。実測: FileExistsError / PermissionError で
止まり、file dialog called=False。実アプリでは「予期しないエラー」に
なり、書ける保存先を指定する手段が無い。

作れなければ黙って別の初期ディレクトリへ落とし、ダイアログは必ず出す。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class LogDirUnwritableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-logdir-")
        self.save_dir = tempfile.mkdtemp(prefix="netbelt-logdir-target-")
        # cwd/logs を「作れない」状態にする: 同名のファイルを置く
        with open(os.path.join(self.dir, "logs"), "w") as f:
            f.write("not a directory")
        self._old_cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._old_cwd)
        patcher = mock.patch("PyQt6.QtWidgets.QMessageBox.information")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        w.append_output("rtrA", "Router#show version\r\n")
        return w

    def test_full_log_save_still_offers_the_file_dialog(self):
        """全ログ保存: logs を作れなくてもダイアログが出て、選んだ先へ書けること。"""
        w = self._widget()
        target = os.path.join(self.save_dir, "out.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "")) as dialog, \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog.exec",
                           return_value=True):
            w.save_current_log()
        self.assertEqual(dialog.call_count, 1,
                         "ファイル選択ダイアログに到達していない")

    def test_full_log_save_does_not_propose_the_unwritable_dir(self):
        """全ログ保存: 初期パスが作れなかった logs の下を指さないこと。"""
        w = self._widget()
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog:
            w.save_current_log()
        initial = dialog.call_args[0][2]
        bad = os.path.join(self.dir, "logs") + os.sep
        self.assertFalse(os.path.abspath(initial).startswith(bad),
                         "作れなかった logs の下を初期パスにしている: %r" % initial)

    def test_recording_start_still_offers_the_file_dialog(self):
        """ログ記録開始: logs を作れなくてもダイアログが出て、記録が始まること。"""
        w = self._widget()
        target = os.path.join(self.save_dir, "rec.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "")) as dialog:
            w.start_log_recording()
        self.assertEqual(dialog.call_count, 1,
                         "ファイル選択ダイアログに到達していない")
        self.assertIn("rtrA", w._log_files, "記録が始まっていない")
        w.stop_log_recording("rtrA")

    def test_logs_dir_is_still_created_when_it_can_be(self):
        """作れるときは従来どおり cwd/logs を作って、そこを初期パスにする。"""
        os.remove(os.path.join(self.dir, "logs"))
        w = self._widget()
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog:
            w.start_log_recording()
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "logs")))
        initial = dialog.call_args[0][2]
        self.assertEqual(os.path.dirname(os.path.abspath(initial)),
                         os.path.join(self.dir, "logs"))


if __name__ == "__main__":
    unittest.main()
