"""「更新が完了しました」と出るのは、本当に差し替わったときだけであることを固定する。

公開直後に報告された症状は「更新が完了しました と表示されるのに、実際には
旧版のまま」だった。実物の配布物（v1.1.0 / v1.1.1 / v1.2.0 / v1.3.0 の
Windows Portable zip）で当時の道筋をなぞり直した結果は次のとおり。

  - v1.1.0 の updater.bat は、xcopy の失敗を「警告」としか出さずに先へ進み、
    差し替えられたかを一度も確かめないまま [6/6] へ進む作りだった。
    アプリを起動したまま 1.1.0→1.2.0 を当てると、NetBelt.exe は
    47,304,322 バイト（sha 8ae105b7…）のまま変わらず、それでも処理は続いた。
  - いまのコード（8d316a7）では、実物どうしの 16 通りの実測で
    「成功表示 かつ 旧版のまま」は一度も起きなかった。

起きなくなったことを、実物の 47MB を使わずに固定しておく。ここでは数百
バイトの exe 代用と zip で同じ道筋を通し、次の 2 つを結びつける。

  1. インストール先の NetBelt.exe を掴んだまま（＝アプリが起動したまま）
     更新すると、成功とは表示されず、exe の中身も変わらないこと。
     掴みが更新の途中で外れたときは、やり直しで差し替わり、そのときだけ
     成功と表示されること。
  2. NetBelt が控えた版（<ZIP>.version）が、見せていた版と食い違うときは、
     updater.bat を起動しないこと（当たっていない更新を「当てた」と
     扱う経路を、入口で塞いでおく）。

掴み方は、起動中の実行イメージと同じ共有（読みだけ許し、書きと削除は
許さない）にしている。実測でも、実物の NetBelt.exe を起動したまま当てると
move /y がこの条件で失敗し、updater は exit 1 で止まった。
"""
import ctypes
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from ctypes import wintypes
from unittest import mock

sys.path.insert(0, "src")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

GENERIC_READ = 0x80000000
GENERIC_EXECUTE = 0x20000000
FILE_SHARE_READ = 0x1
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

OLD_EXE = b"OLD-EXE" + b"\0" * 64
NEW_EXE = b"NEW-EXE" + b"\0" * 64


def _hold_like_a_running_image(path):
    """起動中の実行イメージと同じ共有で開く（読みは可、書きと削除は不可）。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    handle = kernel32.CreateFileW(
        path, GENERIC_READ | GENERIC_EXECUTE, FILE_SHARE_READ,
        None, OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError("掴めない: %s" % path)
    return handle


class SuccessMeansTheExeWasReplacedTest(unittest.TestCase):
    """updater.bat を実際に走らせ、表示と中身を突き合わせる。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_success_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        shutil.copyfile(UPDATER, os.path.join(self.app_dir, "updater.bat"))
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(OLD_EXE)

        self.zip_path = os.path.join(self.base, "NetBelt-apply-test.zip")
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", NEW_EXE)
            z.writestr("zz_bundled.txt", b"new-bundled")
        for suffix, body in ((".sha256", "0" * 64), (".version", "9.9.9")):
            with io.open(self.zip_path + suffix, "w", encoding="ascii") as f:
                f.write(body)

    def _installed_exe(self):
        with io.open(self.app_path, "rb") as f:
            return f.read()

    def _run(self, hold_seconds=None):
        """updater.bat を走らせる。hold_seconds を渡すと exe を掴んでおく。

        None なら掴まない。数値なら、更新が始まってからその秒数だけ掴んで
        手放す（ウイルス対策ソフトの走査や、遅れて終わるアプリに相当）。
        0 未満なら、最後まで掴み続ける。
        """
        handle = None
        if hold_seconds is not None:
            handle = _hold_like_a_running_image(self.app_path)

        def _release():
            time.sleep(hold_seconds)
            ctypes.windll.kernel32.CloseHandle(handle)

        released = None
        if hold_seconds is not None and hold_seconds >= 0:
            released = threading.Thread(target=_release, daemon=True)
            released.start()

        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        try:
            with io.open(out_path, "wb") as out:
                proc = subprocess.Popen(
                    '"%s" "%s" "%s"' % (
                        os.path.join(self.app_dir, "updater.bat"),
                        self.zip_path, self.app_path),
                    stdout=out, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=env)
                code = proc.wait(timeout=180)
        finally:
            if released is not None:
                released.join(30)
            elif handle is not None:
                ctypes.windll.kernel32.CloseHandle(handle)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def test_a_running_app_is_never_reported_as_a_finished_update(self):
        """掴まれたままなら、成功と出さず、exe も旧版のままであること。"""
        code, out = self._run(hold_seconds=-1)

        self.assertNotIn("更新が完了しました", out,
                         "差し替えていないのに完了と表示した:\n" + out)
        self.assertNotEqual(code, 0, out)
        self.assertEqual(self._installed_exe(), OLD_EXE,
                         "掴まれているのに exe が変わった:\n" + out)
        # 一時名の exe を置き去りにしない（次の更新の判断材料を汚さない）
        self.assertEqual(
            sorted(n for n in os.listdir(self.app_dir)
                   if n.startswith("NetBelt.exe")), ["NetBelt.exe"], out)

    def test_a_free_install_is_replaced_and_only_then_reported(self):
        """掴まれていなければ差し替わり、そのときだけ完了と出ること。"""
        code, out = self._run()

        self.assertEqual(code, 0, out)
        self.assertIn("更新が完了しました", out, out)
        self.assertEqual(self._installed_exe(), NEW_EXE,
                         "完了と表示したのに exe が旧版のまま:\n" + out)

    def test_a_hold_that_ends_during_the_update_still_ends_up_replaced(self):
        """途中で掴みが外れたら、やり直して差し替え、そのうえで完了と出ること。"""
        code, out = self._run(hold_seconds=2.5)

        self.assertEqual(code, 0, out)
        self.assertIn("更新が完了しました", out, out)
        self.assertEqual(self._installed_exe(), NEW_EXE,
                         "完了と表示したのに exe が旧版のまま:\n" + out)


class TheRecordedVersionGatesTheApplyTest(unittest.TestCase):
    """控えた版が食い違う更新は、updater.bat まで届かないこと。"""

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
        self.dir = tempfile.mkdtemp(prefix="netbelt_versiongate_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _stage(self, recorded_version):
        body = b"PK\x03\x04 netbelt update"
        zip_path = os.path.join(self.dir, "NetBelt-9.9.9.zip")
        with io.open(zip_path, "wb") as f:
            f.write(body)
        with io.open(zip_path + ".sha256", "w", encoding="ascii") as f:
            f.write(hashlib.sha256(body).hexdigest())
        with io.open(zip_path + ".version", "w", encoding="ascii") as f:
            f.write(recorded_version)
        return zip_path

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt_versiongate_cfg_")
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self)._windows.append(w)
        return w

    def _apply(self, zip_path, shown_version):
        launched = []
        w = self._window()
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("subprocess.Popen",
                        lambda command_line, **kw: launched.append(command_line)
                        or mock.MagicMock()), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn, \
             mock.patch("ui.main_window.QMessageBox.critical"):
            w._apply_pending_update(zip_path, shown_version)
        return launched, warn

    def test_a_version_that_does_not_match_never_reaches_the_updater(self):
        """控えが見せていた版と違うなら、当てずに知らせること。"""
        zip_path = self._stage("1.0.0")

        launched, warn = self._apply(zip_path, "9.9.9")

        self.assertEqual(launched, [],
                         "版が食い違うのに updater.bat を起動した")
        warn.assert_called()

    def test_a_matching_version_is_handed_over(self):
        """一致していれば、これまでどおり渡すこと（取り違えの逆を防ぐ）。"""
        zip_path = self._stage("9.9.9")

        launched, warn = self._apply(zip_path, "9.9.9")

        self.assertEqual(len(launched), 1, "updater.bat を起動していない")
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
