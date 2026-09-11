"""起動時の未適用更新に「はい」と答えたら、アプリが本当に終了することを検証する。

_check_pending_updates は MainWindow.__init__ から呼ばれるので、確認
ダイアログも _apply_pending_update も app.exec() が始まる前に走る。
そこで呼ぶ QApplication.quit() は、イベントループが回っていないと何も
しない（Qt の仕様）。updater.bat は起動され、そのまま exec() に入って
メインウィンドウは表示され続ける。

計測: 「はい」のあと Popen は呼ばれ quit() も 1 回呼ばれたが、
window.isVisible() は exec() の前で True のまま、3 秒たっても exec()
は回り続けていた。updater.bat は 3 秒後に起動中フォルダへ xcopy する
ため、ロックされた NetBelt.exe で失敗し、ロックされていないファイル
だけが先に置き換わる混在が起こりうる。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class PendingUpdateQuitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-pending-")
        with mock.patch("ui.main_window.ConfigManager") as fake,              mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self)._windows.append(w)
        return w

    def _run_event_loop_with_watchdog(self, seconds=2.0):
        """exec() を回し、上限を過ぎたら 42 で抜ける。戻り値と経過秒を返す。"""
        from PyQt6.QtCore import QTimer
        app = self.app
        QTimer.singleShot(int(seconds * 1000), lambda: app.exit(42))
        started = time.monotonic()
        code = app.exec()
        return code, time.monotonic() - started

    def test_applying_before_exec_still_ends_the_event_loop(self):
        """exec() の前に「はい」と答えても、exec() に入った直後に終わること。

        main() は MainWindow() → show() → exec() の順なので、
        起動時の適用は必ず exec() の前に起きる。
        """
        w = self._window()
        w.show()
        with mock.patch("subprocess.Popen") as popen:
            w._apply_pending_update(os.path.join(tempfile.gettempdir(),
                                                 "NetBelt-9.9.9.zip"))
        popen.assert_called_once()

        code, elapsed = self._run_event_loop_with_watchdog()
        self.assertEqual(code, 0,
                         "終了要求が exec() の前に消えていて、番犬が止めた")
        self.assertLess(elapsed, 1.5, "終了までに時間がかかりすぎている")

    def test_a_failed_updater_launch_does_not_quit(self):
        """updater を起動できなかったときは、これまでどおり終了しないこと。"""
        w = self._window()
        with mock.patch("subprocess.Popen", side_effect=OSError("見つかりません")),              mock.patch("ui.main_window.QMessageBox.critical") as critical:
            w._apply_pending_update(os.path.join(tempfile.gettempdir(),
                                                 "NetBelt-9.9.9.zip"))
        critical.assert_called_once()

        code, _ = self._run_event_loop_with_watchdog(0.3)
        self.assertEqual(code, 42, "失敗したのに終了要求が出ている")


if __name__ == "__main__":
    unittest.main()
