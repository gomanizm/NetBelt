"""差し替えを諦めた更新の置き土産を、次の更新が片付けることを検証する。

updater.bat は新しい exe を NetBelt.exe.<目印>.new という一時名でインストール先へ
置いてから move /y で差し替える。その一時名を削除共有なしで掴まれていると、
5 回のやり直しが全部失敗し、後始末の del "!STAGED_PATH!" も同じ理由で失敗する。

実測（検査役 cx5c-verify-release の a_leftover_staged.py、8b0c94e）: exit=1 の
あともインストール先に NetBelt.exe.NetBeltUpdate_1_697.new が残り、掴みを
手放したあとも残ったままだった。一時名は実行ごとに変わるので、失敗のたびに
配布物 1 個ぶん（数十 MB）が積まれていく。誰も見ていないので減ることはない。

直し方: インストール先の排他（目印フォルダ）を取った直後に、インストール先の
NetBelt.exe.*.new を消す。自分の一時名はまだ展開先（TEMP）にあり、排他を
持っている間は他の更新がインストール先へ置くこともないので、そこにあるのは
前の実行の置き去りだけになる。消せないもの（まだ掴まれている等）は黙って飛ばす。
"""
import ctypes
import glob
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

# 詰め物。xcopy が一時名の exe を置いてから move /y へ進むまでの間を広げ、
# 監視側が先に掴めるようにする（test_updater_exe_swap_retry.py と同じ）。
PAD_COUNT = 300


class _StagedExeHolder(object):
    """インストール先に置かれた一時名の exe を、削除共有なしで掴み続ける。"""

    def __init__(self, app_dir):
        self._pattern = os.path.join(app_dir, "NetBelt.exe.*.new")
        self._stop = threading.Event()
        self.handle = None
        self.grabbed = False
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
                    self.grabbed = True
                    return
            time.sleep(0.001)

    def release(self):
        self._stop.set()
        self._thread.join(5)
        if self.handle is not None:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterStagedExeSweepTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_stagedsweep_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        self._write("NetBelt.exe", b"OLD-EXE")

    def _write(self, name, body):
        with io.open(os.path.join(self.app_dir, name), "wb") as f:
            f.write(body)

    def _read(self, name):
        with io.open(os.path.join(self.app_dir, name), "rb") as f:
            return f.read()

    def _staged_left(self):
        return sorted(os.path.basename(p) for p in
                      glob.glob(os.path.join(self.app_dir, "NetBelt.exe.*.new")))

    def _make_zip(self, pad=0):
        path = os.path.join(self.base, "update-%d.zip" % pad)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", b"NEW-EXE-" + b"\0" * 1024)
            for i in range(pad):
                z.writestr("pad/p%04d.bin" % i, b"\0" * 4096)
        return path

    def _run(self, zip_path):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            code = subprocess.Popen(
                '"%s" "%s" "%s"' % (self.updater, zip_path, self.app_path),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env).wait(timeout=300)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def test_a_staged_exe_left_by_an_earlier_run_is_swept_up(self):
        """前の実行が残した一時名の exe を、次の更新が消すこと。"""
        # a_leftover_staged.py が実際に残した名前
        self._write("NetBelt.exe.NetBeltUpdate_1_697.new", b"\0" * 4096)

        code, out = self._run(self._make_zip())

        self.assertEqual(code, 0, out)
        self.assertEqual(self._staged_left(), [],
                         "置き去りの一時名の exe が残った:\n" + out)
        self.assertTrue(self._read("NetBelt.exe").startswith(b"NEW-EXE-"), out)

    def test_the_leftover_of_a_failed_swap_is_gone_after_the_next_update(self):
        """差し替えを諦めた実行の置き土産が、次の更新で消えること。"""
        holder = _StagedExeHolder(self.app_dir)
        holder.start()
        try:
            code, out = self._run(self._make_zip(PAD_COUNT))
            grabbed = holder.grabbed
        finally:
            holder.release()

        self.assertTrue(grabbed, "前提が崩れている（一時名の exe を掴めなかった）:\n"
                        + out)
        self.assertNotEqual(code, 0, "差し替えていないのに成功と報告した:\n" + out)
        left = self._staged_left()
        self.assertEqual(len(left), 1,
                         "前提が崩れている（置き土産ができていない）: %s\n%s"
                         % (left, out))

        code, out = self._run(self._make_zip())

        self.assertEqual(code, 0, out)
        self.assertEqual(self._staged_left(), [],
                         "置き土産が次の更新でも残った:\n" + out)
        self.assertTrue(self._read("NetBelt.exe").startswith(b"NEW-EXE-"), out)

    def test_the_neighbours_of_the_staged_name_are_left_alone(self):
        """名前の似たファイルは消さないこと。"""
        self._write("NetBelt.exe.bak", b"KEEP-BAK")
        self._write("release-notes.new", b"KEEP-NOTES")

        code, out = self._run(self._make_zip())

        self.assertEqual(code, 0, out)
        self.assertEqual(self._read("NetBelt.exe.bak"), b"KEEP-BAK", out)
        self.assertEqual(self._read("release-notes.new"), b"KEEP-NOTES", out)


if __name__ == "__main__":
    unittest.main()
