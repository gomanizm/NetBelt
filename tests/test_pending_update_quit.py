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

_apply_pending_update はソース実行では案内を出して何もしない（同じ
レビューの別の指摘で入った。配布物の ZIP を展開するとリポジトリ直下を
上書きしてしまうため）。ここで見たいのは終了の経路なので、凍結された
exe として動いているふりをする。
"""
import hashlib
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

    def setUp(self):
        """前のテストが残した終了要求を、ここで使い切っておく。

        QApplication はプロセス全体でひとつを共有する（tests/conftest.py）。
        イベントループが回っていないときの QApplication.quit() は捨てられ
        ず、次にイベントを捌くまで保留されたまま残る。
        tests/test_update_source_run.py の
        test_the_pending_path_still_applies_from_a_frozen_build は exec()
        を回さずに _apply_pending_update を呼ぶので、その終了要求がこの
        ファイルまで持ち越され、exec() に入った直後に 0 で戻ってしまう。
        実測: この2ファイルを同じプロセスで走らせると
        test_a_failed_updater_launch_does_not_quit が `0 != 42` で落ち、
        単独では通る。

        ここで捌いておくと、どのテストも「終了要求ゼロ」から始まる。
        テスト本体が出した終了要求はこのあとに起きるので、判定は変わら
        ない。
        """
        for _ in range(5):
            self.app.processEvents()

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

    @staticmethod
    def _as_frozen_build():
        """凍結された exe として動いているふりをする文脈。"""
        return mock.patch.object(sys, "frozen", True, create=True)

    @staticmethod
    def _staged_update():
        """検証済みの更新ファイルを作り、そのパスを返す。

        _apply_pending_update は updater を起動する前に、ZIP の存在と
        ダウンロード時の検証記録を確かめ直す（起動時の適用経路も更新
        ダイアログと同じ確認を通すようにしたため）。ここで見たいのは
        終了の経路なので、その確認を通るファイルを用意する。
        """
        d = tempfile.mkdtemp(prefix="netbelt-pending-zip-")
        zip_path = os.path.join(d, "NetBelt-9.9.9.zip")
        body = b"PK\x03\x04 dummy netbelt update"
        with open(zip_path, "wb") as f:
            f.write(body)
        with open(zip_path + ".sha256", "w", encoding="ascii") as f:
            f.write(hashlib.sha256(body).hexdigest())
        return zip_path

    def _run_event_loop_with_watchdog(self, seconds=2.0):
        """exec() を回し、上限を過ぎたら 42 で抜ける。戻り値と経過秒を返す。

        見張りは戻る前に止める。singleShot で仕掛けたままにすると、exec() が
        先に 0 で戻っても上限の時刻に exec() の外で app.exit(42) が呼ばれ、
        同じプロセスでそのあとに回す QEventLoop.exec() がすぐ戻ってしまう。
        exec() の中で積まれて残った終了要求（表示中のウィンドウが閉じられる
        と Qt がもう 1 つ積む）も、あとで捌かれると同じことになるので取り除く
        （tests/test_pending_update_watchdog_timer.py）。
        """
        from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
        app = self.app
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(lambda: app.exit(42))
        watchdog.start(int(seconds * 1000))
        started = time.monotonic()
        try:
            code = app.exec()
        finally:
            watchdog.stop()
        elapsed = time.monotonic() - started
        QCoreApplication.removePostedEvents(app, QEvent.Type.Quit)
        return code, elapsed

    def test_applying_before_exec_still_ends_the_event_loop(self):
        """exec() の前に「はい」と答えても、exec() に入った直後に終わること。

        main() は MainWindow() → show() → exec() の順なので、
        起動時の適用は必ず exec() の前に起きる。
        """
        w = self._window()
        w.show()
        with self._as_frozen_build(), mock.patch("subprocess.Popen") as popen:
            w._apply_pending_update(self._staged_update())
        popen.assert_called_once()

        code, elapsed = self._run_event_loop_with_watchdog()
        self.assertEqual(code, 0,
                         "終了要求が exec() の前に消えていて、番犬が止めた")
        self.assertLess(elapsed, 1.5, "終了までに時間がかかりすぎている")

    def test_a_failed_updater_launch_does_not_quit(self):
        """updater を起動できなかったときは、これまでどおり終了しないこと。"""
        w = self._window()
        with self._as_frozen_build(),              mock.patch("subprocess.Popen", side_effect=OSError("見つかりません")),              mock.patch("ui.main_window.QMessageBox.critical") as critical:
            w._apply_pending_update(self._staged_update())
        critical.assert_called_once()

        code, _ = self._run_event_loop_with_watchdog(0.3)
        self.assertEqual(code, 42, "失敗したのに終了要求が出ている")


if __name__ == "__main__":
    unittest.main()
