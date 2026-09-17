"""ログ記録中のダイアログに記録したバイト数が出ること、開始と停止で
OK 待ちのメッセージを出さないことを検証する。

記録中ダイアログには保存先と経過時間しか無く、本当に書けているのかが
見えなかった。Tera Term のように、経過時間の下へバイト数を出す。数えるのは
保存先ファイルの実際の大きさ。ログは Windows のテキストモードで開いて
いるので LF が CRLF になり、受信した文字数とは一致しない（実測:
"line 000\\n" ×50 が 500 バイト）。

また、開始時に「ログ記録開始」、停止時に「ログ記録停止」の情報ダイアログが
1 回ずつ出ていた（実測）。複数の機器を記録していると、そのたびに OK を押す
必要がある。記録中ダイアログが出ている・消えることで開始と停止は分かるので、
この 2 つは出さない。書き込みや閉じる処理に失敗したときの警告は残す。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class RecordingDialogShowsSizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _labels(dialog):
        """ダイアログのラベルの文字列を、上から並んでいる順に返す。"""
        from PyQt6.QtWidgets import QLabel
        layout = dialog.layout()
        return [layout.itemAt(i).widget().text()
                for i in range(layout.count())
                if isinstance(layout.itemAt(i).widget(), QLabel)]

    def _size_label(self, dialog):
        found = [t for t in self._labels(dialog) if t.startswith("記録したバイト数")]
        self.assertEqual(len(found), 1, "バイト数の表示が無い: %r"
                         % self._labels(dialog))
        return found[0]

    def test_the_size_is_shown_right_below_the_elapsed_time(self):
        """バイト数は経過時間のすぐ下に並ぶこと。"""
        from ui.dialogs.log_recording_dialog import LogRecordingDialog
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-logsize-"), "a.log")
        with open(path, "wb") as f:
            f.write(b"x" * 1234567)
        dialog = LogRecordingDialog("lab-rtr01", path)
        self.addCleanup(dialog.deleteLater)
        dialog.timer.stop()

        labels = self._labels(dialog)
        elapsed = [i for i, t in enumerate(labels) if t.startswith("経過時間")]
        size = [i for i, t in enumerate(labels) if t.startswith("記録したバイト数")]
        self.assertEqual(len(size), 1, "バイト数の表示が無い: %r" % labels)
        self.assertEqual(size[0], elapsed[0] + 1,
                         "経過時間のすぐ下に無い: %r" % labels)
        self.assertEqual(labels[size[0]], "記録したバイト数: 1,234,567 バイト")

    def test_a_missing_file_does_not_break_the_dialog(self):
        """保存先を読めなくても落ちず、読めないことが分かる表示になること。"""
        from ui.dialogs.log_recording_dialog import LogRecordingDialog
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-logsize-"),
                            "gone", "a.log")
        dialog = LogRecordingDialog("lab-rtr01", path)
        self.addCleanup(dialog.deleteLater)
        dialog.timer.stop()

        dialog.timer.timeout.emit()

        self.assertIn("取得できません", self._size_label(dialog))

    def test_the_size_follows_what_was_written_to_the_file(self):
        """記録中に届いた出力の分だけ、表示が実ファイルの大きさへ追従すること。"""
        from ui.terminal_widget import TerminalWidget
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-logsize-"), "r.log")
        # 後片付けの停止でもメッセージがモーダルで出うるので、最後まで差し替える
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.addCleanup(mock.patch.stopall)
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.addCleanup(w._stop_log_recording_for, "rtrA")
        dialog = w._log_dialogs["rtrA"]
        dialog.timer.stop()

        w.append_output("rtrA", "".join("line %03d 日本語\r\n" % i
                                        for i in range(50)))
        dialog.timer.timeout.emit()

        size = os.path.getsize(path)
        self.assertGreater(size, 0, "前提: ファイルへ書けている")
        self.assertEqual(self._size_label(dialog),
                         "記録したバイト数: {:,} バイト".format(size))


class RecordingStartStopIsQuietTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.path = os.path.join(tempfile.mkdtemp(prefix="netbelt-logquiet-"),
                                 "rtrA.log")
        self.information = mock.patch(
            "PyQt6.QtWidgets.QMessageBox.information").start()
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.addCleanup(mock.patch.stopall)

    def _recording_widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.path, "")):
            w.start_log_recording()
        self.assertIn("rtrA", w._log_files, "前提: 記録が始まっている")
        return w

    def test_starting_a_recording_asks_for_no_ok(self):
        """開始してもメッセージを出さないこと（記録中ダイアログは出る）。"""
        w = self._recording_widget()
        self.assertEqual(self.information.call_count, 0,
                         "開始のたびに OK を押させている")
        self.assertEqual(self.warning.call_count, 0)
        self.assertIn("rtrA", w._log_dialogs, "記録中ダイアログが出ていない")

    def test_stopping_with_the_dialog_button_asks_for_no_ok(self):
        """記録中ダイアログの「記録停止」でもメッセージを出さないこと。"""
        w = self._recording_widget()
        self.information.reset_mock()

        w._log_dialogs["rtrA"]._on_stop()

        self.assertNotIn("rtrA", w._log_files, "前提: 記録が止まっている")
        self.assertEqual(self.information.call_count, 0,
                         "停止のたびに OK を押させている")
        self.assertEqual(self.warning.call_count, 0)

    def test_stopping_from_the_menu_asks_for_no_ok(self):
        """メニューの「ログ記録停止」でもメッセージを出さないこと。"""
        w = self._recording_widget()
        self.information.reset_mock()

        w.stop_log_recording("rtrA")

        self.assertNotIn("rtrA", w._log_files, "前提: 記録が止まっている")
        self.assertEqual(self.information.call_count, 0,
                         "停止のたびに OK を押させている")


if __name__ == "__main__":
    unittest.main()
