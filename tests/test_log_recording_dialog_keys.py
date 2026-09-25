"""記録中ダイアログが見えていることと、記録中であることが常に一致することを検証する。

何が起きていたか（441ea02 で実測。scratchpad\\v132-triage\\recheck-9\\
r1_behaviors.py / r3_focus_keys.py、cx132-termui で再実行）:
- 記録中ダイアログは記録中であることを示す唯一の表示だが、Esc で隠れた
  （visible=False のまま 1 秒タイマーも記録も続き、もう一度「ログ記録開始」
  を選ぶと「既にログ記録中です。」）。× や Alt+F4（closeEvent）と
  QWindow.close も、隠すだけで記録は止めなかった。
- 端末にフォーカスがある状態で記録を始めると、ダイアログが活性化して
  フォーカスが「記録停止」ボタンへ移った。端末へ打つつもりの Esc は
  ダイアログを隠し、Enter・Space は停止ボタンを押して記録を止めた
  （どのキーも端末へ届かない）。

どう直したか（利用者の決定 (ii)）:
- ダイアログは Esc を飲む（QDialog 既定の Esc → reject() → 隠す、を通さない）。
- × や Alt+F4 など、ダイアログを閉じる操作は「記録停止」と同じにする。
  closeEvent で一度だけ stop_requested を出す（停止ボタンから閉じたときは
  もう出しているので出さない）。記録を止める後始末の経路（停止・書き込み
  失敗の中止・タブを閉じる・アプリ終了）から閉じたときも出るが、その時点で
  記録は外れているので何もしない。閉じる要求は断らないので、「今すぐ更新」
  の QApplication.closeAllWindows() も途中で止まらない。
- 記録を始めたら、主窓を活性化し直して端末へフォーカスを戻す。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class LogRecordingDialogKeysTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logdlg-keys-")
        # MainWindow の設定ファイルを一時フォルダへ向ける
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(self.dir))
        ap.start()
        self.addCleanup(ap.stop)
        # 保存先ダイアログの初期フォルダ（cwd\logs）を作らせない
        from ui.terminal_widget import TerminalWidget
        ld = mock.patch.object(TerminalWidget, "_default_log_dir",
                               staticmethod(lambda: self.dir))
        ld.start()
        self.addCleanup(ld.stop)
        self._count = 0

    def _pump(self, rounds=3):
        """遅延削除まで含めて、溜まったイベントを捌く。"""
        from PyQt6.QtCore import QEvent
        for _ in range(rounds):
            self.app.processEvents()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _start(self, start):
        """start()（開始メニューと同じ入口）を保存先ダイアログ抜きで通す。"""
        self._count += 1
        path = os.path.join(self.dir, "rtrA-%d.log" % self._count)
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
             mock.patch("PyQt6.QtWidgets.QMessageBox.information"), \
             mock.patch("PyQt6.QtWidgets.QMessageBox.warning"):
            start()

    def _main_window_recording(self):
        """端末にフォーカスがある主窓から、メニューと同じ入口で記録を始める。"""
        from ui.main_window import MainWindow
        mw = MainWindow()
        self.addCleanup(mw.close)
        mw.show()
        mw.activateWindow()
        tw = mw.terminal_widget
        term = tw.create_terminal_tab("rtrA")
        term.set_input_enabled(True)    # 接続済みと同じくキーを受け付ける
        self._pump()
        term.setFocus()
        self._pump()
        self._start(mw._on_start_log_recording)
        self.assertIn("rtrA", tw._log_files, "前提: 記録が始まっている")
        self.assertIn("rtrA", tw._log_dialogs, "前提: 記録中ダイアログがある")
        self._pump()
        return mw, tw, term

    def _terminal_widget_recording(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.show()
        w.create_terminal_tab("rtrA")
        self._start(w.start_log_recording)
        self.assertIn("rtrA", w._log_files, "前提: 記録が始まっている")
        dialog = w._log_dialogs["rtrA"]
        self._pump()
        self.assertTrue(dialog.isVisible(), "前提: 記録中ダイアログが見えている")
        return w, dialog

    def _key_to_focus(self, key):
        from PyQt6.QtTest import QTest
        target = self.app.focusWidget() or self.app.activeWindow()
        QTest.keyClick(target, key)
        self._pump()

    def test_enter_right_after_starting_goes_to_the_terminal(self):
        """開始直後の Enter が停止ボタンを押さず、端末へ届くこと。"""
        from PyQt6.QtCore import Qt
        mw, tw, term = self._main_window_recording()
        dialog = tw._log_dialogs["rtrA"]
        got = []
        term.key_pressed.connect(got.append)

        self._key_to_focus(Qt.Key.Key_Return)

        self.assertIn("rtrA", tw._log_files, "開始直後の Enter で記録が止まった")
        self.assertTrue(dialog.isVisible())
        self.assertEqual(got, ["\r"], "Enter が端末へ届いていない")

    def test_escape_right_after_starting_goes_to_the_terminal(self):
        """開始直後の Esc が記録中の表示を消さず、端末へ届くこと。"""
        from PyQt6.QtCore import Qt
        mw, tw, term = self._main_window_recording()
        dialog = tw._log_dialogs["rtrA"]
        got = []
        term.key_pressed.connect(got.append)

        self._key_to_focus(Qt.Key.Key_Escape)

        self.assertIn("rtrA", tw._log_files)
        self.assertTrue(dialog.isVisible(),
                        "記録は続いているのに記録中の表示が消えた")
        self.assertEqual(got, ["\x1b"], "Esc が端末へ届いていない")

    def test_escape_on_the_dialog_does_not_hide_it(self):
        """ダイアログにフォーカスがあるときの Esc でも消えず、記録も続くこと。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        w, dialog = self._terminal_widget_recording()
        dialog.activateWindow()
        self._pump()

        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self._pump()

        self.assertTrue(dialog.isVisible(),
                        "記録は続いているのに記録中の表示が消えた")
        self.assertIn("rtrA", w._log_files)
        self.assertTrue(dialog.timer.isActive(), "経過時間の更新が止まった")
        w.stop_log_recording("rtrA")
        self._pump()
        self.assertNotIn("rtrA", w._log_dialogs)

    def test_closing_the_dialog_stops_the_recording(self):
        """× や Alt+F4（closeEvent）で閉じたら、「記録停止」と同じく止まること。"""
        from PyQt6 import sip
        w, dialog = self._terminal_widget_recording()
        handle = w._log_files["rtrA"]

        dialog.close()
        self._pump()

        self.assertNotIn("rtrA", w._log_files,
                         "ダイアログを閉じたのに記録が続いている")
        self.assertTrue(handle.closed, "記録ファイルが閉じられていない")
        self.assertNotIn("rtrA", w._log_dialogs)
        self.assertTrue(sip.isdeleted(dialog), "閉じたダイアログが捨てられていない")
        self.assertFalse(w._terminals["rtrA"]._is_recording)

    def test_the_dialog_does_not_refuse_closing_all_windows(self):
        """closeAllWindows（更新の終了）で使う QWindow.close を断らず、記録も止まること。

        断ると closeAllWindows がそこで止まり、並びによっては主窓の
        closeEvent（記録の書き切り）が通らず、quit() も無視される。
        """
        w, dialog = self._terminal_widget_recording()

        self.assertTrue(dialog.windowHandle().close(),
                        "記録中ダイアログが閉じる要求を断った")
        self._pump()

        self.assertNotIn("rtrA", w._log_files,
                         "記録中の表示が消えたのに記録が続いている")
        self.assertNotIn("rtrA", w._log_dialogs)

    def test_the_stop_button_asks_to_stop_only_once(self):
        """停止ボタンでは停止の要求が 1 回だけ出ること（閉じるときに重ねて出さない）。"""
        from PyQt6.QtWidgets import QPushButton
        w, dialog = self._terminal_widget_recording()
        requests = []
        dialog.stop_requested.connect(requests.append)
        button = [b for b in dialog.findChildren(QPushButton)
                  if b.text() == "記録停止"][0]

        button.click()
        self._pump()

        self.assertEqual(requests, ["rtrA"])
        self.assertNotIn("rtrA", w._log_files)
        self.assertNotIn("rtrA", w._log_dialogs)

    def test_stopping_from_the_menu_then_starting_again_keeps_recording(self):
        """メニューで止めた直後に始め直した記録が、前のダイアログの後始末で止まらないこと。"""
        w, first = self._terminal_widget_recording()

        w.stop_log_recording()          # メニュー（表示中のタブ）から止める
        self._start(w.start_log_recording)
        self._pump()

        self.assertIn("rtrA", w._log_files, "始め直した記録が止まった")
        second = w._log_dialogs.get("rtrA")
        self.assertIsNotNone(second)
        self.assertIsNot(second, first)
        self.assertTrue(second.isVisible())
        w.stop_log_recording("rtrA")
        self._pump()

    def test_a_write_failure_still_discards_the_dialog(self):
        """書き込み失敗で記録を止めたときも、ダイアログが閉じて捨てられること。"""
        from PyQt6 import sip
        w, dialog = self._terminal_widget_recording()

        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning") as warn:
            w._abort_log_recording("rtrA", OSError(28, "No space left on device"))
        self._pump()

        self.assertEqual(warn.call_count, 1)
        self.assertNotIn("rtrA", w._log_files)
        self.assertNotIn("rtrA", w._log_dialogs)
        self.assertTrue(sip.isdeleted(dialog))


if __name__ == "__main__":
    unittest.main()
