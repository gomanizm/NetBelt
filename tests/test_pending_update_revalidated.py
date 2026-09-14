"""起動時の適用経路も、確認ダイアログのあとに ZIP を確かめ直すことを検証する。

更新ダイアログの「適用」は、押された時点で存在・検証済み・版の一致を
もう一度見てから updater を起動する。一方、起動時の未適用更新の経路は
候補を選ぶときに見るだけで、確認ダイアログを閉じたあとは何も見ずに
Popen していた。NetBelt を二重に起動していると、その間に ZIP が消えたり
別の版へ差し替わったりしうる。

両経路が同じ確認を通ること（片方だけ直る形にしないこと）を確かめる。
"""
import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

ZIP_BODY = b"PK\x03\x04 dummy netbelt update"


def stage_update(dirpath, version="9.9.9", body=ZIP_BODY,
                 with_checksum=True, stored_version=None):
    """検証済みの更新ファイル一式を作り、ZIP のパスを返す。"""
    zip_path = os.path.join(dirpath, "NetBelt-%s.zip" % version)
    with open(zip_path, "wb") as f:
        f.write(body)
    if with_checksum:
        with open(zip_path + ".sha256", "w", encoding="ascii") as f:
            f.write(hashlib.sha256(body).hexdigest())
    with open(zip_path + ".version", "w", encoding="ascii") as f:
        f.write(stored_version or version)
    return zip_path


class VerifyBeforeApplyTest(unittest.TestCase):
    """両経路が使う共通の確認そのものの振る舞い。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-revalidate-")

    def test_a_staged_update_passes(self):
        from core.version_manager import VersionManager
        zip_path = stage_update(self.dir)
        self.assertIsNone(
            VersionManager().verify_before_apply(zip_path, "9.9.9"))

    def test_a_missing_file_is_reported(self):
        from core.version_manager import VersionManager
        message = VersionManager().verify_before_apply(
            os.path.join(self.dir, "NetBelt-9.9.9.zip"), "9.9.9")
        self.assertIsNotNone(message)

    def test_an_unverified_file_is_reported(self):
        from core.version_manager import VersionManager
        zip_path = stage_update(self.dir, with_checksum=False)
        self.assertIsNotNone(
            VersionManager().verify_before_apply(zip_path, "9.9.9"))

    def test_a_different_version_is_reported(self):
        from core.version_manager import VersionManager
        zip_path = stage_update(self.dir, stored_version="1.0.0")
        self.assertIsNotNone(
            VersionManager().verify_before_apply(zip_path, "9.9.9"))


class ApplyPendingUpdateRechecksTest(unittest.TestCase):
    """MainWindow._apply_pending_update が Popen の前に確かめ直すこと。"""

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
        self.dir = tempfile.mkdtemp(prefix="netbelt-revalidate-win-")

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-revalidate-cfg-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self)._windows.append(w)
        return w

    def _apply(self, zip_path, expected_version="9.9.9"):
        """凍結された exe のふりをして適用し、(Popen, warning) を返す。"""
        w = self._window()
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("subprocess.Popen") as popen, \
             mock.patch("ui.main_window.QMessageBox.warning") as warn, \
             mock.patch("ui.main_window.QMessageBox.critical"):
            w._apply_pending_update(zip_path, expected_version)
        return popen, warn

    def test_a_staged_update_is_applied(self):
        zip_path = stage_update(self.dir)
        popen, warn = self._apply(zip_path)
        popen.assert_called_once()
        warn.assert_not_called()

    def test_a_vanished_zip_does_not_reach_the_updater(self):
        zip_path = os.path.join(self.dir, "NetBelt-9.9.9.zip")
        popen, warn = self._apply(zip_path)
        popen.assert_not_called()
        warn.assert_called_once()

    def test_an_unverified_zip_does_not_reach_the_updater(self):
        """確認ダイアログの裏で検証記録ごと差し替えられた場合。"""
        zip_path = stage_update(self.dir, with_checksum=False)
        popen, warn = self._apply(zip_path)
        popen.assert_not_called()
        warn.assert_called_once()

    def test_a_swapped_version_does_not_reach_the_updater(self):
        """見せた版と違うものが置かれていたら当てない。"""
        zip_path = stage_update(self.dir, stored_version="1.0.0")
        popen, warn = self._apply(zip_path)
        popen.assert_not_called()
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
