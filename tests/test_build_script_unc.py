"""build.bat の cd が失敗したとき、削除へ進まないことを検証する。

build.bat は先頭で `cd /d "%~dp0"` を実行し、以降の `rmdir /s /q dist`
と `rmdir /s /q build` を相対パスで走らせる。cmd.exe は UNC パスを
カレントディレクトリにできないので、共有フォルダ（\\\\server\\share\\...）
に置いた build.bat を叩くと、この cd は

    CMD does not support UNC paths as current directories.

と出して errorlevel 1 で終わり、カレントは呼び出し元のままになる。
そのまま次の行へ進むため、消えるのは呼び出し元の dist/ と build/ に
なる。tests/test_build_script_cwd.py が守っているのは「cd が成功した
場合」だけで、失敗した場合は素通りしていた。

実測（この環境の管理共有 C$ 経由）:
    DP0=[\\\\localhost\\C$\\...\\proj\\]
    CMD does not support UNC paths as current directories.
    RC=[1]
    CWD=[C:\\Windows]
つまり errorlevel は立つので、cd の直後に見れば止められる。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_BAT = os.path.join(REPO_ROOT, "build.bat")


def _unc(path):
    """ローカルパスを管理共有（C$ など）経由の UNC 表記へ直す。"""
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    if not drive.endswith(":"):
        return None
    return "\\\\localhost\\%s$%s" % (drive[0], rest)


@unittest.skipUnless(os.name == "nt", "cmd.exe が要る")
class BuildScriptUncTest(unittest.TestCase):
    """UNC から起動して、実際に cd を失敗させる。PyInstaller は呼ばせない。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_unc_")
        self.addCleanup(shutil.rmtree, self.base, True)

        # スクリプトの置き場所（共有フォルダに置かれたリポジトリの代わり）
        self.proj = os.path.join(self.base, "proj")
        os.makedirs(os.path.join(self.proj, "dist"))
        os.makedirs(os.path.join(self.proj, "build"))
        shutil.copyfile(BUILD_BAT, os.path.join(self.proj, "build.bat"))

        # 呼び出し元（dist/ と build/ を持つ別プロジェクト）
        self.other = os.path.join(self.base, "other")
        os.makedirs(os.path.join(self.other, "dist", "sub"))
        os.makedirs(os.path.join(self.other, "build"))
        self._write(os.path.join(self.other, "dist", "sub", "important.txt"),
                    "keep me")
        self._write(os.path.join(self.other, "build", "obj.o"), "keep me")

        # PATH を差し替え、python の代わりに即終了する偽物を呼ばせる。
        # 本物が見つかると PyInstaller のビルドが始まり、数分かかる。
        self.shim = os.path.join(self.base, "shim")
        os.makedirs(self.shim)
        io.open(os.path.join(self.shim, "python.bat"), "w",
                encoding="ascii", newline="\r\n").write(
                    "@echo off\r\necho fake python %*\r\nexit /b 9009\r\n")

        self.unc_bat = None
        unc_proj = _unc(self.proj)
        if unc_proj and os.path.isdir(unc_proj):
            self.unc_bat = unc_proj + "\\build.bat"

    def _write(self, path, text):
        io.open(path, "w", encoding="ascii").write(text)

    def _run(self):
        if not self.unc_bat:
            self.skipTest("この環境では管理共有経由の UNC パスを使えない")
        out_path = os.path.join(self.base, "out.txt")
        env = {
            "PATH": self.shim,
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "SystemRoot": os.environ["SystemRoot"],
            "COMSPEC": os.environ.get("COMSPEC", "cmd.exe"),
            "TEMP": self.base,
            "TMP": self.base,
        }
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s"' % self.unc_bat,
                cwd=self.other, env=env,
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            code = proc.wait(timeout=120)
        return code, io.open(out_path, encoding="utf-8",
                             errors="replace").read()

    def test_the_callers_dist_and_build_survive_a_failed_cd(self):
        """cd が失敗しても、呼び出し元の dist/ と build/ を消さないこと。"""
        code, out = self._run()

        self.assertTrue(
            os.path.exists(os.path.join(self.other, "dist", "sub",
                                        "important.txt")),
            "cd に失敗したのに呼び出し元の dist/ を消している:\n" + out)
        self.assertTrue(
            os.path.exists(os.path.join(self.other, "build", "obj.o")),
            "cd に失敗したのに呼び出し元の build/ を消している:\n" + out)
        self.assertNotEqual(code, 0,
                            "自分の置き場所へ移れないのに成功として終わっている:\n"
                            + out)


if __name__ == "__main__":
    unittest.main()
