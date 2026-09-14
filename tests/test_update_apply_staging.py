"""適用の直前に確かめたバイト列が、そのまま展開されることを確認する。

検査役の実測: verify_before_apply が通ってから updater.bat が
Expand-Archive で開き直すまでに約3.0〜3.5秒あり、Python 側が渡すのは
パス文字列だけで写しもロックも取らない。その間に ZIP を別の有効な ZIP へ
置き換えると、.sha256 と一致しないバイト列が展開・インストール・再起動
まで通った。

    verify_before_apply  = None
    t=+1.01s swapped
    is_verified_update   = False
    rc = 0
      |  更新が完了しました！
    installed VERSION_MARKER.txt = 'EVIL-SWAPPED-v9.9.9'

窓の実測: delay=0.2/1.5/2.5/3.0s は差し替え成功、3.5s 以降は失敗。

この試験では「updater.bat を起動した直後に元の ZIP を差し替える」ことで
同じ状況を作り、updater.bat へ渡されたパスの中身が差し替え後のものに
なっていないことを見る。
"""
import hashlib
import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, "src")

GOOD_BODY = b"PK\x03\x04 good netbelt update"
EVIL_BODY = b"PK\x03\x04 evil netbelt update (swapped)"
VERSION = "9.9.9"


def stage_update(dirpath, version=VERSION, body=GOOD_BODY):
    """検証済みの更新ファイル一式を作り、ZIP のパスを返す。"""
    zip_path = os.path.join(dirpath, "NetBelt-%s.zip" % version)
    io.open(zip_path, "wb").write(body)
    io.open(zip_path + ".sha256", "w", encoding="ascii").write(
        hashlib.sha256(body).hexdigest())
    io.open(zip_path + ".version", "w", encoding="ascii").write(version)
    return zip_path


def swap(zip_path, body=EVIL_BODY):
    """別プロセスが有効な別の ZIP へ置き換えたのと同じ状態にする。

    .sha256 は元のまま（攻撃側が控えを直さなくても通ってしまうこと自体が
    指摘の内容）。
    """
    other = zip_path + ".evil"
    io.open(other, "wb").write(body)
    os.replace(other, zip_path)


def zip_argument(command_line):
    """updater_command が組んだコマンド行から ZIP のパスを取り出す。"""
    return command_line.split('" "')[1]


class StageForApplyTest(unittest.TestCase):
    """確かめた写しを返す、共通の口そのものの振る舞い。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-staging-")

    def _manager(self):
        from core.version_manager import VersionManager
        return VersionManager()

    def test_the_staged_copy_is_not_affected_by_a_later_swap(self):
        zip_path = stage_update(self.dir)

        staged, problem = self._manager().stage_for_apply(zip_path, VERSION)
        self.assertIsNone(problem)
        swap(zip_path)

        self.assertEqual(io.open(staged, "rb").read(), GOOD_BODY,
                         "確かめたあとの差し替えが、当たる中身に届いている")
        self.assertTrue(self._manager().is_verified_update(staged))

    def test_an_unverified_zip_is_refused(self):
        """検証を通らないものは、これまでどおり文言を返すこと。"""
        zip_path = stage_update(self.dir)
        os.remove(zip_path + ".sha256")

        staged, problem = self._manager().stage_for_apply(zip_path, VERSION)

        self.assertIsNone(staged)
        self.assertIsNotNone(problem)

    def test_a_mismatching_version_is_refused(self):
        zip_path = stage_update(self.dir)

        staged, problem = self._manager().stage_for_apply(zip_path, "1.0.0")

        self.assertIsNone(staged)
        self.assertIsNotNone(problem)


class PendingApplyHandsOverAVerifiedCopyTest(unittest.TestCase):
    """起動時の未適用更新の経路。"""

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
        self.dir = tempfile.mkdtemp(prefix="netbelt-staging-win-")

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-staging-cfg-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self)._windows.append(w)
        return w

    def test_a_swap_right_after_launch_does_not_reach_the_updater(self):
        zip_path = stage_update(self.dir)
        handed = []

        def popen(command_line, **kwargs):
            handed.append(command_line)
            # updater.bat は NetBelt の終了を 3 秒待つ。その間に起きること
            swap(zip_path)
            return mock.MagicMock()

        w = self._window()
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("subprocess.Popen", popen), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn, \
             mock.patch("ui.main_window.QMessageBox.critical") as crit:
            w._apply_pending_update(zip_path, VERSION)

        warn.assert_not_called()
        crit.assert_not_called()
        self.assertEqual(len(handed), 1, "updater を起動していない")
        applied = zip_argument(handed[0])
        self.assertEqual(
            io.open(applied, "rb").read(), GOOD_BODY,
            "確かめた内容と違うバイト列が updater.bat へ渡っている")


class DialogApplyHandsOverAVerifiedCopyTest(unittest.TestCase):
    """更新ダイアログの「適用」の経路。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-staging-dlg-")

    def test_a_swap_right_after_launch_does_not_reach_the_updater(self):
        from ui.dialogs.update_dialog import UpdateDialog

        zip_path = stage_update(self.dir)
        handed = []

        def popen(command_line, **kwargs):
            handed.append(command_line)
            swap(zip_path)
            return mock.MagicMock()

        me = types.SimpleNamespace(
            downloaded_zip_path=zip_path,
            update_info={"version": VERSION},
            UPDATE_NOW=UpdateDialog.UPDATE_NOW,
            done=lambda code: None)

        with mock.patch("ui.dialogs.update_dialog.running_from_source",
                        return_value=False), \
             mock.patch("subprocess.Popen", popen), \
             mock.patch("ui.dialogs.update_dialog.QMessageBox.warning") as warn, \
             mock.patch("ui.dialogs.update_dialog.QMessageBox.critical") as crit:
            UpdateDialog._on_apply_clicked(me)

        warn.assert_not_called()
        crit.assert_not_called()
        self.assertEqual(len(handed), 1, "updater を起動していない")
        applied = zip_argument(handed[0])
        self.assertEqual(
            io.open(applied, "rb").read(), GOOD_BODY,
            "確かめた内容と違うバイト列が updater.bat へ渡っている")


if __name__ == "__main__":
    unittest.main()
