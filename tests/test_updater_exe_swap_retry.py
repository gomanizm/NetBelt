"""インストール先に置いた一時名の exe を短く掴まれても、差し替えを諦めないことを検証する。

updater.bat の [5/6] は、展開した NetBelt.exe を一時名
（NetBelt.exe.<目印>.new）へ改名し、xcopy でインストール先へ置いてから
move /y で NetBelt.exe へ差し替える。展開先での改名（ren）には、
ウイルス対策ソフトの短い走査を待ってやり直す処理が入った
（tests/test_updater_exe_rename_blocked.py）。ところが最後の move /y は
1 回きりだった。

実測（cx5b-verify-release の r01_move_hold.py、62357d1）: xcopy が置いた
一時名の exe を、読み書きは共有するが削除は共有しないハンドルで
1.5 秒だけ掴むと、move /y がその間に走って失敗し、updater は exit 1 で
「NetBelt.exe を差し替えられませんでした」と表示した。同梱の他の
ファイルは既に新しい版で、NetBelt.exe だけが旧版のまま残った。
走査はすぐ終わるのに、更新はやり直しになっていた。

直し方: move /y も ren と同じ形で、約 1 秒（ping -n 2 127.0.0.1）おきに
最大 5 回やり直す。それでも差し替えられないときの後始末と案内は
これまでと同じ。

ここでは隔離したインストール先を監視し、xcopy が置いた一時名の exe を
削除共有なしで掴み、xcopy が終わってから一定時間持ち続ける。
"""
import ctypes
import glob
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

# 詰め物の数。xcopy がこれを全部置き終えた直後に move /y が走る。
PAD_COUNT = 300


class _StagedExeHolder(object):
    """インストール先に置かれた一時名の exe を、削除共有なしで掴む。

    読み取りと書き込みは共有するので、xcopy の書き込みは通る。
    削除（改名・上書きの move を含む）だけが共有違反で失敗する。
    """

    def __init__(self, app_dir, hold_after_copy=None):
        self._pattern = os.path.join(app_dir, "NetBelt.exe.*.new")
        # xcopy が最後に置く詰め物。これが揃った直後に move /y が走る。
        self._last_pad = os.path.join(app_dir, "pad", "p%04d.bin" % (PAD_COUNT - 1))
        # None なら更新が終わるまで持ち続ける。秒数なら、xcopy が
        # 終わってからその秒数だけ持って手放す（短い走査に相当）。
        self._hold_after_copy = hold_after_copy
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.handle = None
        self.grabbed = False
        self.released = False
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
                    with self._lock:
                        self.handle = handle
                        self.grabbed = True
                    if self._hold_after_copy is not None:
                        self._release_after_copy()
                    return
            time.sleep(0.001)

    def _release_after_copy(self):
        while not self._stop.is_set():
            try:
                if os.path.getsize(self._last_pad) == 4096:
                    break
            except OSError:
                pass
            time.sleep(0.005)
        else:
            return
        self._stop.wait(self._hold_after_copy)
        self._close()
        self.released = True

    def _close(self):
        with self._lock:
            if self.handle is not None:
                _kernel32.CloseHandle(self.handle)
                self.handle = None

    def release(self):
        self._stop.set()
        self._thread.join(5)
        self._close()


class UpdaterExeSwapRetryTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_swapretry_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        self._write("NetBelt.exe", b"OLD-EXE")
        self._write("zz_bundled.txt", b"old-bundled")

    def _write(self, name, body):
        with io.open(os.path.join(self.app_dir, name), "wb") as f:
            f.write(body)

    def _read(self, name):
        with io.open(os.path.join(self.app_dir, name), "rb") as f:
            return f.read()

    def _run_holding_the_staged_exe(self, hold_after_copy=None):
        zip_path = os.path.join(self.base, "update.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", b"NEW-EXE-" + b"\0" * 1024)
            z.writestr("zz_bundled.txt", b"new-bundled")
            # 一時名の exe を置いてから move /y までの間を空ける詰め物。
            # 監視側が move /y より先に一時名の exe を掴めるようにするため。
            for i in range(PAD_COUNT):
                z.writestr("pad/p%04d.bin" % i, b"\0" * 4096)

        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        holder = _StagedExeHolder(self.app_dir, hold_after_copy)
        try:
            with io.open(out_path, "wb") as out:
                holder.start()
                proc = subprocess.Popen(
                    '"%s" "%s" "%s"' % (self.updater, zip_path, self.app_path),
                    stdout=out, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=env)
                code = proc.wait(timeout=180)
            held = holder.grabbed
        finally:
            holder.release()
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            out = f.read()
        self.assertTrue(held, "前提が崩れている（一時名の exe を掴めなかった）:\n"
                        + out)
        if hold_after_copy is not None:
            self.assertTrue(holder.released,
                            "前提が崩れている（コピーの完了を見届けていない）:\n"
                            + out)
        return code, out

    def test_a_short_scan_of_the_staged_exe_is_waited_out(self):
        """一時名の exe を短く掴まれただけなら、待ってやり直して差し替えること。"""
        code, out = self._run_holding_the_staged_exe(hold_after_copy=1.5)

        self.assertEqual(code, 0, "一時的な走査で差し替えを諦めた:\n" + out)
        self.assertNotIn("差し替えられませんでした", out, out)
        self.assertTrue(self._read("NetBelt.exe").startswith(b"NEW-EXE-"), out)
        self.assertEqual(self._read("zz_bundled.txt"), b"new-bundled", out)
        self.assertEqual(
            sorted(n for n in os.listdir(self.app_dir)
                   if n.startswith("NetBelt.exe")),
            ["NetBelt.exe"], "一時名の exe を置き去りにした:\n" + out)

    def test_a_staged_exe_held_throughout_still_fails_honestly(self):
        """掴まれたままなら、これまでどおり失敗を伝え、旧 exe を残すこと。"""
        code, out = self._run_holding_the_staged_exe()

        self.assertNotEqual(code, 0, "差し替えていないのに成功と報告した:\n" + out)
        self.assertIn("NetBelt.exe を差し替えられませんでした", out, out)
        self.assertNotIn("更新が完了しました", out, out)
        self.assertEqual(self._read("NetBelt.exe"), b"OLD-EXE", out)


if __name__ == "__main__":
    unittest.main()
