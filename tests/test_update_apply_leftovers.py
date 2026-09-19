"""updater.bat を起動できなかった適用が、適用用の写しを更新フォルダに残さないこと。

適用の直前に、stage_for_apply が検証済みの ZIP の写し（NetBelt-apply-*.zip と
その .sha256・.version）を作り、updater.bat にはその写しを渡す
（tests/test_update_apply_staging.py）。ところが写しを作った後で updater.bat が
見つからない・パスに cmd が意味を変える文字（& など）がある・Popen が失敗する、
のいずれかで戻る経路に、写しの削除が無かった。

実測（検証役 release-05）: 本物の UpdateDialog の「更新を適用」を 3 回押すと、
インストール先に & を含む場合・updater.bat が無い場合・Popen が OSError を
出す場合のいずれも、NetBelt-apply-*.zip と控えの組が 3 組（9 ファイル）
増えた。起動時の経路（MainWindow._apply_pending_update）も & を含む
インストール先で 1 組残した。残った写しは検証記録つきの .zip なので、
未適用の更新の候補にも並んだ。掃除は起動時の更新チェックの中だけで、
それを無効にしていると残り続ける。1 回失敗するごとに配布 ZIP 1 個分が
%TEMP%\\NetBeltUpdates に増える。

直し方: updater.bat の起動を VersionManager.launch_updater にまとめ、起動
できなかったとき（updater_command の拒否・Popen の失敗）は写しを片付けて
から例外を投げ直す。2 つの適用経路は同じ関数を通るので、片方だけ直る形に
ならない。更新ダイアログは updater.bat の存在確認を写しを作る前へ移した。
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, "src")

VERSION = "9.9.9"
BODY = b"PK\x03\x04" + b"x" * 4096


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.version_manager import VersionManager
        self.base = tempfile.mkdtemp(prefix="netbelt-apply-leftovers-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.upd_dir = os.path.join(self.base, "NetBeltUpdates")
        os.makedirs(self.upd_dir)
        patcher = mock.patch.object(VersionManager, "UPDATE_DIR", self.upd_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.zip_path = os.path.join(self.upd_dir, "NetBelt-%s.zip" % VERSION)
        with io.open(self.zip_path, "wb") as f:
            f.write(BODY)
        with io.open(self.zip_path + ".sha256", "w", encoding="ascii") as f:
            f.write(hashlib.sha256(BODY).hexdigest())
        with io.open(self.zip_path + ".version", "w", encoding="ascii") as f:
            f.write(VERSION)

    def _install(self, name, with_updater=True):
        """ダミーのインストール先を作り、NetBelt.exe のパスを返す。"""
        folder = os.path.join(self.base, name)
        os.makedirs(folder)
        exe = os.path.join(folder, "NetBelt.exe")
        with io.open(exe, "wb") as f:
            f.write(b"MZ")
        if with_updater:
            with io.open(os.path.join(folder, "updater.bat"), "wb") as f:
                f.write(b"@echo off\r\n")
        return exe

    def _staged_copies(self):
        return sorted(n for n in os.listdir(self.upd_dir)
                      if n.startswith("NetBelt-apply-"))


class DialogApplyLeftoversTest(_Base):
    """更新ダイアログの「更新を適用」の経路。"""

    def _click_apply(self, exe, popen_error=None, times=3):
        from ui.dialogs.update_dialog import UpdateDialog
        dialog = UpdateDialog(None, {"version": VERSION,
                                     "download_url": "https://example.com/x.zip"})
        self.addCleanup(dialog.deleteLater)
        dialog.downloaded_zip_path = self.zip_path
        popen = mock.MagicMock(side_effect=popen_error)
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", exe), \
                mock.patch("subprocess.Popen", popen), \
                mock.patch("ui.dialogs.update_dialog.QMessageBox.critical") as crit, \
                mock.patch("ui.dialogs.update_dialog.QMessageBox.warning") as warn, \
                mock.patch("PyQt6.QtWidgets.QApplication.quit"):
            for _ in range(times):
                dialog._show_completed_phase()
                dialog.apply_button.click()
                self.app.processEvents()
        return popen, crit, warn

    def test_an_ampersand_in_the_install_folder_leaves_no_copy(self):
        popen, crit, warn = self._click_apply(self._install("R&D"))

        self.assertEqual(crit.call_count, 3, "前提: 3 回とも拒否される")
        popen.assert_not_called()
        self.assertEqual(self._staged_copies(), [],
                         "拒否された適用の写しが更新フォルダに残った")

    def test_a_missing_updater_leaves_no_copy(self):
        popen, crit, warn = self._click_apply(
            self._install("noupdater", with_updater=False))

        self.assertEqual(crit.call_count, 3, "前提: 3 回とも拒否される")
        popen.assert_not_called()
        self.assertEqual(self._staged_copies(), [],
                         "拒否された適用の写しが更新フォルダに残った")

    def test_a_failed_launch_leaves_no_copy(self):
        popen, crit, warn = self._click_apply(
            self._install("popenfail"), popen_error=OSError(2, "injected"))

        self.assertEqual(crit.call_count, 3, "前提: 3 回とも失敗が伝わる")
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(self._staged_copies(), [],
                         "起動できなかった適用の写しが更新フォルダに残った")

    def test_a_successful_launch_keeps_the_copy_for_the_updater(self):
        """起動できたときは、updater.bat が使う写しを消さないこと。"""
        popen, crit, warn = self._click_apply(self._install("ok"), times=1)

        crit.assert_not_called()
        warn.assert_not_called()
        popen.assert_called_once()
        copies = self._staged_copies()
        self.assertEqual(len(copies), 3, copies)
        handed = popen.call_args[0][0].split('" "')[1]
        self.assertTrue(os.path.exists(handed),
                        "updater.bat へ渡した写しを消してしまった")


class PendingApplyLeftoversTest(_Base):
    """起動時の未適用更新（MainWindow._apply_pending_update）の経路。"""

    def _apply(self, exe, popen_error=None):
        from ui.main_window import MainWindow
        popen = mock.MagicMock(side_effect=popen_error)
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", exe), \
                mock.patch("subprocess.Popen", popen), \
                mock.patch("ui.main_window.QMessageBox.critical") as crit, \
                mock.patch("ui.main_window.QMessageBox.warning") as warn, \
                mock.patch("PyQt6.QtWidgets.QApplication.quit"):
            MainWindow._apply_pending_update(types.SimpleNamespace(),
                                             self.zip_path, VERSION)
            # 成功時は singleShot(0) で終了を予約するので、ここで流しておく
            self.app.processEvents()
        return popen, crit, warn

    def test_an_ampersand_in_the_install_folder_leaves_no_copy(self):
        popen, crit, warn = self._apply(self._install("R&D"))

        crit.assert_called_once()
        popen.assert_not_called()
        self.assertEqual(self._staged_copies(), [],
                         "拒否された適用の写しが更新フォルダに残った")

    def test_a_failed_launch_leaves_no_copy(self):
        popen, crit, warn = self._apply(
            self._install("popenfail"), popen_error=OSError(2, "injected"))

        crit.assert_called_once()
        popen.assert_called_once()
        self.assertEqual(self._staged_copies(), [],
                         "起動できなかった適用の写しが更新フォルダに残った")

    def test_a_successful_launch_keeps_the_copy_for_the_updater(self):
        popen, crit, warn = self._apply(self._install("ok"))

        crit.assert_not_called()
        warn.assert_not_called()
        popen.assert_called_once()
        self.assertEqual(len(self._staged_copies()), 3)
        handed = popen.call_args[0][0].split('" "')[1]
        self.assertTrue(os.path.exists(handed),
                        "updater.bat へ渡した写しを消してしまった")


if __name__ == "__main__":
    unittest.main()
