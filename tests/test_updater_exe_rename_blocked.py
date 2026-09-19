"""展開した NetBelt.exe を一時名へ改名できないとき、旧 exe を失わないことを検証する。

updater.bat は新しい exe を直接上書きせず、展開先で NetBelt.exe を一時名
（NetBelt.exe.<目印>.new）へ改名してから xcopy で置き、最後に改名で
差し替える（tests/test_updater_exe_staging.py）。ところがその改名（ren）の
結果を確かめていなかった。

実測（検証役 release-01）: 展開が終わった NetBelt.exe を、別のプロセスが
「読み書きは共有するが削除は共有しない」ハンドルで開いていると
（ウイルス対策ソフトの走査・インデクサ・Python の open() と同じ共有モード）、
ren だけが失敗し、xcopy は元の名前のまま exe をインストール先へ直接
上書きした。据わった NetBelt.exe の先頭は b'NEW-EXE-'、同梱ファイルも
新しい版だった。それでいて updater は exit 1 で
「更新ファイルに NetBelt.exe が含まれていません」
「NetBelt.exe は旧版のままですが…」と、事実と逆のことを表示した。
600MB の exe でコピー開始 50ms 後に更新を止めると、インストール先の
NetBelt.exe は先頭も末尾も 0x00 の起動できないファイルになり、旧 exe は
失われた。一時名で置く仕組みが、まさに防ぐはずだった壊れ方である。

直し方: ren の直後に一時名ができたかを確かめ、できていなければ少し
待って数回やり直す。それでも移せなければ、xcopy の前（インストール先に
何も書いていない段階）で「何も変えていない」と伝えて中止する。
「含まれていません」の判定も xcopy の前へ移し、exe の無い更新で同梱
ファイルだけが置き換わることもなくした。

ここでは隔離した TEMP を監視し、展開された NetBelt.exe を削除共有なしで
掴み続けて、ren だけを確実に失敗させる。
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


class _ExtractedExeHolder(object):
    """展開された NetBelt.exe を見つけしだい、削除共有なしで開いて持ち続ける。

    読み取りと書き込みは共有するので、展開や xcopy の読み出しは通る。
    削除（改名を含む）だけが共有違反で失敗する。
    """

    # updater.bat が改名の直前に出す行
    MARKER = "コピー先:"

    def __init__(self, temp_dir, out_path, hold_after_marker=None):
        self._pattern = os.path.join(
            temp_dir, "NetBeltUpdate_*", "zip", "NetBelt.exe")
        self._out_path = out_path
        self._stop = threading.Event()
        # None なら更新が終わるまで持ち続ける。秒数なら、updater が改名の
        # 直前の行を出してからその秒数だけ持って手放す（ウイルス対策
        # ソフトの短い走査に相当）。最初の改名は必ず失敗する。
        self._hold_after_marker = hold_after_marker
        self.handle = None
        self.grabbed = False
        self.released_after_marker = False
        self._lock = threading.Lock()
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
                    if self._hold_after_marker is not None:
                        self._release_after_marker()
                    return
            time.sleep(0.001)

    def _release_after_marker(self):
        while not self._stop.is_set():
            with io.open(self._out_path, encoding="utf-8",
                         errors="replace") as f:
                if self.MARKER in f.read():
                    break
            time.sleep(0.01)
        else:
            return
        self._stop.wait(self._hold_after_marker)
        self._close()
        self.released_after_marker = True

    def _close(self):
        with self._lock:
            if self.handle is not None:
                _kernel32.CloseHandle(self.handle)
                self.handle = None

    def release(self):
        self._stop.set()
        self._thread.join(5)
        self._close()


class UpdaterExeRenameBlockedTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_renblk_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        # 再起動先は NetBelt.exe。改名された exe は更新を当てずに中止されるため
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        self._write("NetBelt.exe", b"OLD-EXE")
        self._write("zz_bundled.txt", b"old-bundled")

    def _write(self, name, body):
        with io.open(os.path.join(self.app_dir, name), "wb") as f:
            f.write(body)

    def _read(self, name):
        with io.open(os.path.join(self.app_dir, name), "rb") as f:
            return f.read()

    def _run_holding_the_extracted_exe(self, hold_after_marker=None):
        zip_path = os.path.join(self.base, "update.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", b"NEW-EXE-" + b"\0" * 1024)
            z.writestr("zz_bundled.txt", b"new-bundled")
            # exe の後も展開がしばらく続くようにする詰め物。
            # 監視側が ren より先に exe を掴めるようにするため。
            for i in range(300):
                z.writestr("pad/p%04d.bin" % i, b"\0" * 4096)

        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        holder = _ExtractedExeHolder(self.temp, out_path, hold_after_marker)
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
        self.assertTrue(held, "前提が崩れている（展開した exe を掴めなかった）:\n"
                        + out)
        if hold_after_marker is not None:
            self.assertTrue(holder.released_after_marker,
                            "前提が崩れている（改名の前に手放していない）:\n" + out)
        return code, out

    def test_a_blocked_rename_leaves_the_install_folder_untouched(self):
        """ren が失敗したら、xcopy の前に中止して何も書き換えないこと。"""
        code, out = self._run_holding_the_extracted_exe()

        self.assertNotEqual(code, 0, "exe を一時名へ移せていないのに成功と報告した\n"
                            + out)
        self.assertEqual(self._read("NetBelt.exe"), b"OLD-EXE",
                         "一時名を経ずに exe を直接上書きした:\n" + out)
        self.assertEqual(self._read("zz_bundled.txt"), b"old-bundled",
                         "中止したのに同梱ファイルを書き換えた:\n" + out)
        self.assertEqual(
            sorted(n for n in os.listdir(self.app_dir)
                   if n.startswith("NetBelt.exe")),
            ["NetBelt.exe"], "一時名の exe を置き去りにした:\n" + out)

    def test_a_blocked_rename_is_not_reported_as_a_missing_exe(self):
        """exe は入っているのに「含まれていません」と言わず、無変更を伝えること。"""
        code, out = self._run_holding_the_extracted_exe()

        self.assertNotEqual(code, 0, out)
        self.assertNotIn("含まれていません", out,
                         "exe は入っているのに『含まれていない』と報告した:\n" + out)
        self.assertIn("何も変えていません", out,
                      "インストール先を変えていないことを伝えていない:\n" + out)

    def test_a_short_scan_is_waited_out_and_the_update_applies(self):
        """短い走査で一時的に改名できないだけなら、待ってやり直して更新を当てること。"""
        code, out = self._run_holding_the_extracted_exe(hold_after_marker=1.5)

        self.assertEqual(code, 0, "一時的な走査で更新を諦めた:\n" + out)
        self.assertTrue(self._read("NetBelt.exe").startswith(b"NEW-EXE-"), out)
        self.assertEqual(self._read("zz_bundled.txt"), b"new-bundled", out)
        self.assertEqual(
            sorted(n for n in os.listdir(self.app_dir)
                   if n.startswith("NetBelt.exe")),
            ["NetBelt.exe"], "一時名の exe を置き去りにした:\n" + out)


if __name__ == "__main__":
    unittest.main()
