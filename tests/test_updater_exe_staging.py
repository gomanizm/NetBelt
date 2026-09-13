"""updater.bat が、コピーに失敗しても旧 NetBelt.exe を失わないことを検証する。

以前は展開したファイルを xcopy でインストール先へ直接上書きしていた。
CopyFile 系はまず宛先を切り詰めてから順に書くので、途中で止まると
（更新コンソールを閉じる・電源断・ディスク障害）旧 exe は既に無く、
末尾がゼロ埋めの不完全な exe だけが NetBelt.exe として残る。実測
（inspector, release #4）: 0.05 秒で止めた場合 `final_size=2147483648
head=b'NEW-EXE-' tail=b'\\x00'*8`、旧 'OLD-EXE' は消えていた。

途中停止は手元で再現しにくいので、ここでは「コピーが失敗として
報告されたとき、旧 exe が無傷で残っている」ことで判定する。xcopy は
最初の失敗で止まるため、NetBelt.exe より後ろに並ぶファイルを施錠して
おくと、直接上書きの版は exe を置き換えた後で失敗を報告する。つまり
「失敗と言いながら exe だけ新しい」半端な状態になる。一時名で置いて
から改名で差し替える版なら、旧 exe は最後の改名まで触られない。
"""
import ctypes
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from ctypes import wintypes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def _lock_exclusively(path):
    """他プロセスからの読み書き・削除を一切許さずに開く（起動中の exe と同じ）。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        path, GENERIC_READ, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle == INVALID_HANDLE_VALUE:
        raise OSError("施錠できない: %s" % path)
    return handle


def _unlock(handle):
    ctypes.windll.kernel32.CloseHandle(handle)


class UpdaterExeStagingTest(unittest.TestCase):
    """tests/test_updater_script.py と同じ隔離された実行環境で走らせる。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_stage_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self.app_path = os.path.join(self.app_dir, "dummy_app.exe")
        shutil.copyfile(
            os.path.join(os.environ["SystemRoot"], "System32",
                         "rundll32.exe"), self.app_path)
        self._write("NetBelt.exe", "old")

    def _write(self, name, text):
        io.open(os.path.join(self.app_dir, name), "w",
                encoding="ascii", newline="").write(text)

    def _read(self, name):
        path = os.path.join(self.app_dir, name)
        if not os.path.exists(path):
            return None
        return io.open(path, encoding="ascii").read()

    def _run(self, entries):
        zip_path = os.path.join(self.base, "update.zip")
        with zipfile.ZipFile(zip_path, "w") as z:
            for name, body in entries.items():
                z.writestr(name, body)
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s" "%s" "%s"' % (self.updater, zip_path, self.app_path),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
            code = proc.wait(timeout=180)
        return code, io.open(out_path, encoding="utf-8",
                             errors="replace").read()

    def test_a_copy_that_fails_reports_failure_with_the_old_exe_intact(self):
        """失敗と報告するなら、旧 exe は無傷で残っていること。

        NetBelt.exe の後ろに並ぶファイルを施錠して、コピーを途中で
        失敗させる。直接上書きの版は exe を置き換えた後で失敗するので、
        「失敗」の報告と「exe は新しい」が同時に成り立ってしまう。
        """
        self._write("zz_notes.txt", "old-notes")
        handle = _lock_exclusively(os.path.join(self.app_dir, "zz_notes.txt"))
        self.addCleanup(_unlock, handle)

        code, out = self._run({"NetBelt.exe": "new",
                               "zz_notes.txt": "new-notes"})

        self.assertNotEqual(code, 0, "コピーが失敗したのに成功と報告した\n" + out)
        self.assertEqual(self._read("NetBelt.exe"), "old",
                         "失敗と報告しながら exe を置き換えている:\n" + out)
        self.assertFalse(os.path.exists(
            os.path.join(self.app_dir, "NetBelt.exe.new")),
            "失敗したのに一時名の exe が残っている:\n" + out)

    def test_a_running_app_keeps_its_exe_and_the_failure_is_reported(self):
        """アプリが起動したまま（exe が施錠）でも、旧 exe を壊さないこと。"""
        handle = _lock_exclusively(os.path.join(self.app_dir, "NetBelt.exe"))
        self.addCleanup(_unlock, handle)

        code, out = self._run({"NetBelt.exe": "new"})

        self.assertNotEqual(code, 0, "差し替えできていないのに成功と報告した\n" + out)
        _unlock(handle)
        self.assertEqual(self._read("NetBelt.exe"), "old", out)

    def test_a_failed_exe_swap_says_the_other_files_are_already_new(self):
        """exe の差し替えに失敗したら、混在状態になったことを伝えること。

        xcopy が置くのは NetBelt.exe.new という別名なので、exe が
        施錠されていても同梱の他のファイルはそのまま新版へ入れ替わる。
        その後の改名が失敗したときに「差し替えられませんでした」と
        だけ出すと、利用者は旧 exe と新しい同梱ファイルが混在した
        状態に気づかないまま使い続ける。直接上書きしていた頃は
        xcopy が exe で止まったので、他のファイルは触られなかった。
        """
        self._write("zz_notes.txt", "old-notes")
        handle = _lock_exclusively(os.path.join(self.app_dir, "NetBelt.exe"))
        self.addCleanup(_unlock, handle)

        code, out = self._run({"NetBelt.exe": "new",
                               "zz_notes.txt": "new-notes"})

        self.assertNotEqual(code, 0, "差し替えできていないのに成功と報告した\n"
                            + out)
        self.assertEqual(self._read("zz_notes.txt"), "new-notes",
                         "前提が崩れている（他のファイルが入れ替わっていない）:\n"
                         + out)
        self.assertIn("他のファイル", out,
                      "他のファイルだけ新版になったことを伝えていない:\n" + out)

    def test_a_successful_update_leaves_no_staging_file(self):
        """成功したら、一時名の exe を置き去りにしないこと。"""
        code, out = self._run({"NetBelt.exe": "new", "README.txt": "r"})

        self.assertEqual(code, 0, out)
        self.assertEqual(self._read("NetBelt.exe"), "new", out)
        self.assertEqual(
            sorted(n for n in os.listdir(self.app_dir)
                   if n.startswith("NetBelt.exe")),
            ["NetBelt.exe"], "一時名の exe が残っている:\n" + out)


if __name__ == "__main__":
    unittest.main()
