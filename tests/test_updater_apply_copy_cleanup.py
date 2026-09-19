"""updater.bat が何も当てずに中止したとき、適用用の写しを残さないことを検証する。

NetBelt は更新を当てる直前に、検証した ZIP の写し（更新フォルダの
NetBelt-apply-*.zip）とその控え（.sha256 / .version）を作り、写しの
パスを updater.bat へ渡す（VersionManager.stage_for_apply）。元の ZIP は
更新フォルダに残している。updater.bat が写しを消すのは、更新を当て
終えたときだけだった。

実測（cx5b-verify-release の r05_probe.py、62357d1）: 実行ファイルの名前が
NetBelt.exe でない環境で更新を 3 回試すと、updater.bat は 3 回とも
インストール先に何も書かずに exit 1 で中止し、更新フォルダには写しが
控えごと 3 組（9 ファイル）残った。どれも検証記録つきの .zip なので、
未適用の更新の候補（get_pending_update_files）にも並ぶ。元の ZIP が
残っているので、写しは当て直しの材料にもならない。展開の失敗・
exe の欠落・一時名への改名の失敗でも同じだった。

直し方: インストール先に何も書く前に中止する道では、渡された ZIP の
名前が NetBelt-apply- で始まるときに限り、ZIP と .sha256 / .version を
消してから止まる。利用者が手で渡した ZIP は、これまでどおり消さない。
インストール先を書き換えはじめた後の失敗（xcopy・差し替え）と、更新を
当て終えたあとの扱いは変えていない。
"""
import ctypes
import glob
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from ctypes import wintypes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

_kernel32 = ctypes.windll.kernel32
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

VERSION = "9.9.9"
APPLY_NAME = "NetBelt-apply-k3j9x2.zip"
ORIGINAL_NAME = "NetBelt-%s.zip" % VERSION


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, body in entries:
            z.writestr(name, body)
    return buf.getvalue()


class _ExtractedExeHolder(object):
    """展開された NetBelt.exe を見つけしだい、削除共有なしで掴み続ける。

    tests/test_updater_exe_rename_blocked.py と同じ手口で、展開先での
    一時名への改名（ren）だけを失敗させる。
    """

    def __init__(self, temp_dir):
        self._pattern = os.path.join(
            temp_dir, "NetBeltUpdate_*", "zip", "NetBelt.exe")
        self._stop = threading.Event()
        self.handle = None
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self):
        self._thread.start()

    def _watch(self):
        while not self._stop.is_set():
            for path in glob.glob(self._pattern):
                handle = _kernel32.CreateFileW(
                    path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    None, OPEN_EXISTING, 0, None)
                if handle and handle != INVALID_HANDLE_VALUE:
                    self.handle = handle
                    return
            time.sleep(0.001)

    def release(self):
        self._stop.set()
        self._thread.join(5)
        if self.handle is not None:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


class UpdaterApplyCopyCleanupTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_applycopy_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.upd = os.path.join(self.base, "NetBeltUpdates")
        os.makedirs(self.upd)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(b"OLD-EXE")
        self.good_zip = _zip_bytes([("NetBelt.exe", b"NEW-EXE"),
                                    ("zz_bundled.txt", b"new-bundled")])
        # 元の ZIP。どの中止でも手を付けてはいけない
        self._put_set(ORIGINAL_NAME, self.good_zip)

    def _put_set(self, name, body):
        """ZIP と控え（.sha256 / .version）の組を更新フォルダへ置く。"""
        path = os.path.join(self.upd, name)
        with io.open(path, "wb") as f:
            f.write(body)
        with io.open(path + ".sha256", "w", encoding="ascii") as f:
            f.write(hashlib.sha256(body).hexdigest())
        with io.open(path + ".version", "w", encoding="ascii") as f:
            f.write(VERSION)
        return path

    def _updates(self):
        return sorted(os.listdir(self.upd))

    def _run(self, command):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            code = subprocess.Popen(
                command, stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env).wait(timeout=180)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def _run_updater(self, zip_path, app_path=None):
        return self._run('"%s" "%s" "%s"' % (
            self.updater, zip_path, app_path or self.app_path))

    def _assert_aborted_untouched(self, code, out):
        self.assertNotEqual(code, 0, "中止したのに成功と報告した:\n" + out)
        with io.open(self.app_path, "rb") as f:
            self.assertEqual(f.read(), b"OLD-EXE",
                             "中止したのにインストール先を書き換えた:\n" + out)

    def _assert_only_the_original_is_left(self, out):
        self.assertEqual(
            self._updates(),
            [ORIGINAL_NAME, ORIGINAL_NAME + ".sha256", ORIGINAL_NAME + ".version"],
            "適用用の写しが残ったか、元の ZIP まで消した:\n" + out)

    def test_a_renamed_exe_abort_drops_the_apply_copy(self):
        """実行ファイル名が違うため中止したとき、写しを控えごと消すこと。"""
        copy = self._put_set(APPLY_NAME, self.good_zip)
        renamed = os.path.join(self.app_dir, "NetBelt-old.exe")
        with io.open(renamed, "wb") as f:
            f.write(b"OLD-EXE")

        code, out = self._run_updater(copy, renamed)

        self._assert_aborted_untouched(code, out)
        self.assertIn("NetBelt.exe ではありません", out, out)
        self._assert_only_the_original_is_left(out)

    def test_a_broken_zip_abort_drops_the_apply_copy(self):
        """展開に失敗して中止したとき、写しを控えごと消すこと。"""
        copy = self._put_set(APPLY_NAME, b"this is not a zip archive")

        code, out = self._run_updater(copy)

        self._assert_aborted_untouched(code, out)
        self.assertIn("展開に失敗しました", out, out)
        self._assert_only_the_original_is_left(out)

    def test_an_update_without_the_exe_drops_the_apply_copy(self):
        """更新に NetBelt.exe が無くて中止したとき、写しを控えごと消すこと。"""
        copy = self._put_set(APPLY_NAME, _zip_bytes(
            [("zz_bundled.txt", b"new-bundled")]))

        code, out = self._run_updater(copy)

        self._assert_aborted_untouched(code, out)
        self.assertIn("含まれていません", out, out)
        self._assert_only_the_original_is_left(out)

    def test_a_blocked_rename_abort_drops_the_apply_copy(self):
        """展開した exe を一時名へ移せずに中止したとき、写しを控えごと消すこと。"""
        copy = self._put_set(APPLY_NAME, _zip_bytes(
            [("NetBelt.exe", b"NEW-EXE-" + b"\0" * 1024)]
            + [("pad/p%04d.bin" % i, b"\0" * 4096) for i in range(300)]))
        holder = _ExtractedExeHolder(self.temp)
        holder.start()
        try:
            code, out = self._run_updater(copy)
            held = holder.handle is not None
        finally:
            holder.release()

        self.assertTrue(held, "前提が崩れている（展開した exe を掴めなかった）:\n"
                        + out)
        self._assert_aborted_untouched(code, out)
        self.assertIn("一時名へ移せませんでした", out, out)
        self._assert_only_the_original_is_left(out)

    def test_a_temp_folder_failure_drops_the_apply_copy(self):
        """展開先を作れずに中止したとき、写しを控えごと消すこと。

        作業フォルダは通常は親が md で確保するので、ここだけは再入後の
        入口（第3引数 --utf8）を直接叩き、ファイルの下を作業フォルダに
        指定して mkdir を失敗させる。コードページは親と同じく先に決める。
        """
        copy = self._put_set(APPLY_NAME, self.good_zip)
        blocker = os.path.join(self.base, "not-a-folder")
        with io.open(blocker, "wb") as f:
            f.write(b"x")

        code, out = self._run('cmd /d /s /c "chcp 65001 >nul & "%s" "%s" "%s" '
                              '--utf8 "%s" "%s""' % (
                                  self.updater, copy, self.app_path,
                                  self.app_dir, os.path.join(blocker, "work")))

        self._assert_aborted_untouched(code, out)
        self.assertIn("一時ディレクトリの作成に失敗しました", out, out)
        self._assert_only_the_original_is_left(out)

    def test_a_missing_apply_copy_drops_its_leftover_sidecars(self):
        """写しの ZIP が無くて中止したときも、残った控えを消すこと。"""
        copy = self._put_set(APPLY_NAME, self.good_zip)
        os.remove(copy)

        code, out = self._run_updater(copy)

        self._assert_aborted_untouched(code, out)
        self.assertIn("ZIPファイルが見つかりません", out, out)
        self._assert_only_the_original_is_left(out)

    def test_a_zip_handed_over_by_hand_is_kept(self):
        """写しでない ZIP は、中止してもこれまでどおり消さないこと。"""
        renamed = os.path.join(self.app_dir, "NetBelt-old.exe")
        with io.open(renamed, "wb") as f:
            f.write(b"OLD-EXE")

        code, out = self._run_updater(
            os.path.join(self.upd, ORIGINAL_NAME), renamed)

        self._assert_aborted_untouched(code, out)
        self._assert_only_the_original_is_left(out)


if __name__ == "__main__":
    unittest.main()
