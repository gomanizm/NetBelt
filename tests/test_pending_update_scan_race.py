"""未適用 ZIP の走査中にファイルが消えても、起動が止まらないことを確認する。

検査役の実測: 検証を通った直後（is_verified_update が True を返した直後）に
別プロセスが ZIP を消すと、os.path.getmtime が FileNotFoundError を投げ、
その例外が MainWindow.__init__ から main() まで抜けて app.exec() に
到達しなかった（400 回中 100 回の実競合でも再現）。

    File "...\\src\\ui\\main_window.py", line 2236, in _check_pending_updates
      file_age_hours = (datetime.now().timestamp() - os.path.getmtime(zip_path)) / 3600
    FileNotFoundError: [WinError 2] 指定されたファイルが見つかりません。

走査中にファイルが消えるのは正常な競合なので、その候補だけを飛ばす。
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock

sys.path.insert(0, "src")

# conftest.py の autouse fixture が MainWindow._check_for_updates_on_startup を
# 丸ごと差し替えるため、試験中にクラスから取ると本物が手に入らない。
# 差し替えが始まる前（収集時の import）に本物を控える。
from ui.main_window import MainWindow as _MainWindow  # noqa: E402

_REAL_CHECK_ON_STARTUP = _MainWindow._check_for_updates_on_startup

BODY_A = b"PK" + b"payload-for-9.9.1" * 50
BODY_B = b"PK" + b"payload-for-9.9.2" * 50


class PendingScanRaceTest(unittest.TestCase):
    """走査の途中で ZIP が消える場面。"""

    def setUp(self):
        from core.version_manager import VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-scan-race-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.object(
            VersionManager, "UPDATE_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _place(self, version, body):
        path = os.path.join(self.tmp, "NetBelt-%s.zip" % version)
        io.open(path, "wb").write(body)
        io.open(path + ".sha256", "w").write(hashlib.sha256(body).hexdigest())
        io.open(path + ".version", "w").write(version)
        return path

    def _check(self, vanish=()):
        """_check_pending_updates を呼ぶ。

        vanish に挙げたパスは、検証を通った直後に消える（＝別プロセスが
        消したのと同じ状態にする）。
        """
        from PyQt6.QtWidgets import QMessageBox
        from core.version_manager import VersionManager
        from ui.main_window import MainWindow

        applied = []
        asked = []
        real_verify = VersionManager.is_verified_update

        def verified_then_vanish(self_mgr, zip_path):
            result = real_verify(self_mgr, zip_path)
            if result and zip_path in vanish:
                os.remove(zip_path)
            return result

        def question(parent, title, text, *a, **k):
            asked.append(text)
            return QMessageBox.StandardButton.Yes

        me = types.SimpleNamespace(
            _apply_pending_update=lambda path, version=None: applied.append(path))
        with unittest.mock.patch.object(
                VersionManager, "is_verified_update", verified_then_vanish):
            with unittest.mock.patch(
                    "ui.main_window.QMessageBox.question", question):
                with unittest.mock.patch(
                        "core.version_manager.running_from_source",
                        return_value=False):
                    MainWindow._check_pending_updates(me)
        return applied, asked

    def test_a_zip_removed_during_the_scan_does_not_raise(self):
        path = self._place("9.9.9", BODY_A)

        applied, asked = self._check(vanish=(path,))

        self.assertEqual(applied, [], "消えた ZIP を勧めた: %s" % asked)

    def test_the_surviving_zip_is_still_offered(self):
        """消えた候補で走査を打ち切らず、残った候補は勧めること。"""
        gone = self._place("9.9.1", BODY_A)
        alive = self._place("9.9.2", BODY_B)

        applied, asked = self._check(vanish=(gone,))

        self.assertEqual(applied, [alive],
                         "残っている新しい ZIP を勧めていない: %s" % asked)


class StartupSurvivesUpdateCheckFailureTest(unittest.TestCase):
    """更新チェックの失敗で起動そのものを止めないこと。

    実測では例外が main.py:90 の MainWindow() まで抜け、window.show() と
    app.exec() に到達しなかった（MODE=race RESULT='raised'）。
    """

    def test_a_failing_pending_check_does_not_abort_startup(self):
        def boom():
            raise FileNotFoundError(2, "指定されたファイルが見つかりません。")

        me = types.SimpleNamespace(
            config_manager=types.SimpleNamespace(
                get_check_on_startup=lambda: True,
                get_github_token=lambda: None,
                get_skipped_version=lambda: None),
            _check_pending_updates=boom,
            update_available=types.SimpleNamespace(emit=lambda info: None))

        with unittest.mock.patch("ui.main_window.VersionManager") as vm:
            vm.return_value.check_for_updates.return_value = None
            vm.return_value.cleanup_old_updates.return_value = 0
            _REAL_CHECK_ON_STARTUP(me)


if __name__ == "__main__":
    unittest.main()
