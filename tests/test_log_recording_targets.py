"""ログ記録の停止が、押したダイアログの機器に効くことを検証する。

LogRecordingDialog は機器ごとに1枚出るが、stop_requested は引数を持たず、
emit も自分の device_name を伝えていなかった。受け手の stop_log_recording
は tab_widget.currentIndex() から対象を引き直すため、停止するのは
「押したダイアログの機器」ではなく「いま表示しているタブ」になる。

2台を同時に記録していると、A のダイアログで停止を押しても閉じられるのは
B のファイルで、A は _log_files に残ったまま記録が続く。しかも A の
ダイアログは自分で close() するので、記録中であることが画面から消える。
利用者は止めたつもりで作業を続け、その内容がディスクに残り続ける。

同じ根で _close_tab も _log_files / _log_dialogs に触れていないため、
記録中のタブを閉じるとファイルハンドルが開いたまま残る（Windows では
ファイルがロックされたままになる）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class LogRecordingTargetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-log-")
        # offscreen では通知ダイアログを閉じる相手がいない
        patcher = mock.patch("PyQt6.QtWidgets.QMessageBox.information")
        self.information = patcher.start()
        self.addCleanup(patcher.stop)

    def _widget_recording_two_devices(self):
        """rtrA と rtrB の両方を記録中にし、rtrB を表示した状態にする。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("rtrA")
        w.create_terminal_tab("rtrB")

        for name in ("rtrA", "rtrB"):
            self._select(w, name)
            path = os.path.join(self.dir, name + ".log")
            with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                            return_value=(path, "")):
                w.start_log_recording()

        self._select(w, "rtrB")     # 表示中は rtrB
        return w

    @staticmethod
    def _select(w, tab_name):
        for i in range(w.tab_widget.count()):
            if w.tab_widget.tabText(i) == tab_name:
                w.tab_widget.setCurrentIndex(i)
                return
        raise AssertionError("タブが無い: %s" % tab_name)

    def test_both_devices_start_out_recording(self):
        """前提: 2台とも記録中になっていること。"""
        w = self._widget_recording_two_devices()
        self.assertIn("rtrA", w._log_files)
        self.assertIn("rtrB", w._log_files)

    def test_stopping_from_a_dialog_stops_that_device(self):
        """rtrA のダイアログで止めたら、止まるのは rtrA であること。"""
        w = self._widget_recording_two_devices()
        w._log_dialogs["rtrA"]._on_stop()
        self.assertNotIn("rtrA", w._log_files,
                         "押したダイアログの機器が止まっていない")

    def test_stopping_one_device_leaves_the_visible_one_recording(self):
        """表示中の rtrB を巻き添えにしないこと。"""
        w = self._widget_recording_two_devices()
        w._log_dialogs["rtrA"]._on_stop()
        self.assertIn("rtrB", w._log_files,
                      "止めていない機器の記録が打ち切られている")

    def test_stopping_from_a_dialog_closes_that_devices_file(self):
        """止めた機器のファイルハンドルを閉じること。"""
        w = self._widget_recording_two_devices()
        handle = w._log_files["rtrA"]
        w._log_dialogs["rtrA"]._on_stop()
        self.assertTrue(handle.closed, "ファイルが開いたまま残っている")

    def test_the_menu_still_stops_the_visible_tab(self):
        """引数無しの呼び出し（メニュー・右クリック）は表示中のタブを止める。"""
        w = self._widget_recording_two_devices()
        w.stop_log_recording()
        self.assertNotIn("rtrB", w._log_files,
                         "表示中のタブが止まっていない")
        self.assertIn("rtrA", w._log_files,
                      "表示していない機器まで止めている")


class ClosingATabStopsItsRecordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-log-close-")
        patcher = mock.patch("PyQt6.QtWidgets.QMessageBox.information")
        self.information = patcher.start()
        self.addCleanup(patcher.stop)

    def _widget_recording(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("rtrA")
        for i in range(w.tab_widget.count()):
            if w.tab_widget.tabText(i) == "rtrA":
                w.tab_widget.setCurrentIndex(i)
                index = i
        path = os.path.join(self.dir, "rtrA.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        return w, index

    def test_closing_a_recording_tab_closes_its_file(self):
        """記録中のタブを閉じたら、ファイルを閉じること。

        開いたままだと Windows ではファイルがロックされたまま残る。
        """
        w, index = self._widget_recording()
        handle = w._log_files["rtrA"]
        w._close_tab(index)
        self.assertTrue(handle.closed, "タブを閉じてもファイルが開いたまま")

    def test_closing_a_recording_tab_forgets_it(self):
        """閉じたタブの記録を辞書に残さないこと。"""
        w, index = self._widget_recording()
        w._close_tab(index)
        self.assertNotIn("rtrA", w._log_files)
        self.assertNotIn("rtrA", w._log_dialogs,
                         "宙に浮いたダイアログが残っている")


if __name__ == "__main__":
    unittest.main()
