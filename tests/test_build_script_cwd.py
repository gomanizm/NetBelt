"""build.bat が、呼び出し元のカレントディレクトリを壊さないことを検証する。

build.bat は先頭で `rmdir /s /q dist` と `rmdir /s /q build` を相対パスで
実行していた。%~dp0 へ移動していないので、別のプロジェクトをカレントに
して絶対パスで呼ぶと、そのプロジェクトの dist/ と build/ を黙って再帰
削除する（実測: 呼び出し元の other/dist/sub/important.txt と
other/build/obj.o が消え、スクリプト自身の隣の dist/build は残った）。

dist/ と build/ を持つのは Python・PyInstaller・JS のプロジェクトに
共通なので、消える相手はいくらでもある。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_BAT = os.path.join(REPO_ROOT, "build.bat")


class BuildScriptCwdTest(unittest.TestCase):
    """実際に cmd.exe で走らせて確かめる。PyInstaller は呼ばせない。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_build_")
        self.addCleanup(shutil.rmtree, self.base, True)

        # スクリプトの置き場所（リポジトリ直下の代わり）
        self.proj = os.path.join(self.base, "proj")
        os.makedirs(os.path.join(self.proj, "dist"))
        os.makedirs(os.path.join(self.proj, "build"))
        shutil.copyfile(BUILD_BAT, os.path.join(self.proj, "build.bat"))
        self._write(os.path.join(self.proj, "dist", "stale.txt"), "stale")
        self._write(os.path.join(self.proj, "build", "stale.o"), "stale")

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

    def _write(self, path, text):
        io.open(path, "w", encoding="ascii").write(text)

    def _run(self):
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
                '"%s"' % os.path.join(self.proj, "build.bat"),
                cwd=self.other, env=env,
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            proc.wait(timeout=120)
        return io.open(out_path, encoding="utf-8", errors="replace").read()

    def test_the_callers_dist_and_build_survive(self):
        """別のディレクトリから呼んでも、そこの dist/ と build/ を消さないこと。"""
        out = self._run()

        self.assertTrue(
            os.path.exists(os.path.join(self.other, "dist", "sub",
                                        "important.txt")),
            "呼び出し元の dist/ を消している:\n" + out)
        self.assertTrue(
            os.path.exists(os.path.join(self.other, "build", "obj.o")),
            "呼び出し元の build/ を消している:\n" + out)

    def test_the_scripts_own_dist_and_build_are_cleaned(self):
        """掃除の対象は、スクリプト自身の隣の dist/ と build/ であること。"""
        out = self._run()

        self.assertFalse(os.path.exists(os.path.join(self.proj, "dist")),
                         "自分の隣の dist/ が残っている:\n" + out)
        self.assertFalse(os.path.exists(os.path.join(self.proj, "build")),
                         "自分の隣の build/ が残っている:\n" + out)


if __name__ == "__main__":
    unittest.main()
