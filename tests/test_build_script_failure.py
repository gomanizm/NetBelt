"""build.bat が、失敗したビルドを成功として報告しないことを検証する。

build.bat は PyInstaller の終了コードを一切見ず、`dist\\NetBelt.exe` が
「在るかどうか」だけで成否を決めていた。そのため

* PyInstaller がエラーで落ちたとき
* クリーンアップの `rmdir /s /q dist` が失敗し、前回ビルドの exe が
  そのまま残ったとき

の組み合わせで、古い exe を指して「ビルド成功！」と表示し、終了コードも
0 を返す。手渡しで配ると、旧版を新版として渡してしまう。

exe が1つも出来ていない素直な失敗でも終了コードは 0 のままなので、
build.bat を他のスクリプトから呼んでも失敗を検出できない。

公式リリースは .github/workflows/build-release.yml の別経路で作られる
ので影響を受けない。壊れるのは build.bat を使う手元のビルドだけ。

PyInstaller は本物を呼ばせない。build.bat の cd 後のカレント（＝スクリプト
の置き場所）に偽の PyInstaller パッケージを置き、`python -m` が sys.path の
先頭にカレントを入れる性質でそちらを先に拾わせる。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_BAT = os.path.join(REPO_ROOT, "build.bat")
UPDATER_BAT = os.path.join(REPO_ROOT, "updater.bat")


@unittest.skipUnless(os.name == "nt", "cmd.exe が要る")
class BuildScriptFailureTest(unittest.TestCase):
    """実際に cmd.exe で走らせて確かめる。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_buildfail_")
        self.addCleanup(shutil.rmtree, self.base, True)

        # スクリプトの置き場所（リポジトリ直下の代わり）
        self.proj = os.path.join(self.base, "proj")
        os.makedirs(os.path.join(self.proj, "build"))
        shutil.copyfile(BUILD_BAT, os.path.join(self.proj, "build.bat"))
        # updater.bat が無いと copy が失敗し、別の失敗経路に入ってしまう
        shutil.copyfile(UPDATER_BAT, os.path.join(self.proj, "updater.bat"))

        self.dist = os.path.join(self.proj, "dist")
        self.exe = os.path.join(self.dist, "NetBelt.exe")

    def _write_fake_pyinstaller(self, body):
        """偽の PyInstaller を置く。本物を呼ぶと数分かかる。"""
        pkg = os.path.join(self.proj, "PyInstaller")
        os.makedirs(pkg)
        io.open(os.path.join(pkg, "__init__.py"), "w",
                encoding="ascii", newline="\n").write("")
        io.open(os.path.join(pkg, "__main__.py"), "w",
                encoding="ascii", newline="\n").write(body)

    def _run(self):
        out_path = os.path.join(self.base, "out.txt")
        env = dict(os.environ)
        # build.bat が呼ぶ python を、テストを走らせている python にする
        env["PATH"] = (os.path.dirname(os.path.abspath(sys.executable))
                       + os.pathsep + env.get("PATH", ""))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTHONPATH", None)
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s"' % os.path.join(self.proj, "build.bat"),
                cwd=self.base, env=env,
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            code = proc.wait(timeout=300)
        return code, io.open(out_path, encoding="utf-8",
                             errors="replace").read()

    def test_a_pyinstaller_that_fails_is_not_reported_as_success(self):
        """PyInstaller が失敗したら、失敗として終わること。"""
        self._write_fake_pyinstaller(
            "import sys\n"
            "sys.stderr.write('fake PyInstaller: build failed\\n')\n"
            "raise SystemExit(1)\n")

        code, out = self._run()

        self.assertIn("fake PyInstaller", out,
                      "前提が崩れている: 偽の PyInstaller が呼ばれていない:\n" + out)
        self.assertNotEqual(code, 0,
                            "PyInstaller が失敗したのに成功として終わっている:\n"
                            + out)

    def test_a_stale_exe_left_by_a_failed_clean_is_not_reported_as_success(self):
        """掃除できなかった前回の exe を、今回の成果物として扱わないこと。"""
        os.makedirs(self.dist)
        io.open(self.exe, "w", encoding="ascii").write("stale exe")
        # 掴んだままにして rmdir を失敗させる
        held = io.open(self.exe, "rb")
        self.addCleanup(held.close)

        self._write_fake_pyinstaller(
            "import sys\n"
            "sys.stderr.write('fake PyInstaller: build failed\\n')\n"
            "raise SystemExit(1)\n")

        code, out = self._run()

        self.assertTrue(os.path.exists(self.exe),
                        "前提が崩れている: 掴んだままの exe が消えた:\n" + out)
        self.assertNotEqual(code, 0,
                            "前回の exe が残っているだけなのに成功として終わっている:\n"
                            + out)

    def test_a_build_that_produces_nothing_is_not_reported_as_success(self):
        """exe が1つも出来ていなければ、失敗として終わること。"""
        self._write_fake_pyinstaller(
            "import sys\n"
            "sys.stderr.write('fake PyInstaller: produced nothing\\n')\n")

        code, out = self._run()

        self.assertFalse(os.path.exists(self.exe),
                         "前提が崩れている: 偽の PyInstaller が exe を作っている:\n"
                         + out)
        self.assertNotEqual(code, 0,
                            "exe が無いのに成功として終わっている:\n" + out)

    def test_a_successful_build_still_succeeds(self):
        """本当に成功したビルドは、これまで通り成功として終わること。"""
        self._write_fake_pyinstaller(
            "import os\n"
            "os.makedirs('dist', exist_ok=True)\n"
            "open(os.path.join('dist', 'NetBelt.exe'), 'wb').write(b'exe')\n")

        code, out = self._run()

        self.assertEqual(code, 0, "成功したビルドを失敗にしている:\n" + out)
        self.assertTrue(
            os.path.exists(os.path.join(self.dist, "updater.bat")),
            "updater.bat が dist へ入っていない:\n" + out)


if __name__ == "__main__":
    unittest.main()
