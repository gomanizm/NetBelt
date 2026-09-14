"""build.bat の日本語の行が、断片としてコマンド実行されないことを検証する。

build.bat は BOM なしの UTF-8 だが、コードページを決めていなかった。
cmd.exe はバッチファイルをコンソールのコードページ（日本語環境では
CP932）で読み、しかも次に読む位置をバイトで持つため、UTF-8 の日本語が
CP932 として復号された時点で長さが合わなくなり、読み取り位置がずれる。
ずれた先では行が途中から始まり、残りがコマンドとして実行される。

実測では updater.bat のコピーに失敗する経路で

    '・・・できませんでした。' は、内部コマンドまたは外部コマンド、
    操作可能なプログラムまたはバッチ ファイルとして認識されていません。

が 2 行出た。断片の実行は errorlevel を立てるので、これが
`if errorlevel 1` の手前で起きれば、直後の判定の結果を反転させうる。

コンソールのコードページはプロセスではなくコンソールに属し、build.bat
自身が変えたものが後続の実行へ残る。素で走らせると「直前に何を走らせたか」
で結果が変わってしまうので、各実行の直前に CP932 へ固定する。

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

if os.name == "nt":
    import ctypes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_BAT = os.path.join(REPO_ROOT, "build.bat")
UPDATER_BAT = os.path.join(REPO_ROOT, "updater.bat")

# 「'...' は、内部コマンドまたは外部コマンド〜として認識されていません」
# の言語ごとの目印。出力のコードページが分からないので、cp932 と utf-8 の
# 両方で読んでから探す。
NOT_RECOGNIZED = (
    "認識されていません",  # 認識されていません
    "is not recognized",
)
SHIFT_JIS = 932


@unittest.skipUnless(os.name == "nt", "cmd.exe が要る")
class BuildScriptEncodingTest(unittest.TestCase):
    """実際に cmd.exe で走らせて、余計なコマンドが走らないことを見る。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_buildenc_")
        self.addCleanup(shutil.rmtree, self.base, True)

        self.proj = os.path.join(self.base, "proj")
        os.makedirs(os.path.join(self.proj, "build"))
        shutil.copyfile(BUILD_BAT, os.path.join(self.proj, "build.bat"))

        self.dist = os.path.join(self.proj, "dist")
        self.exe = os.path.join(self.dist, "NetBelt.exe")

        pkg = os.path.join(self.proj, "PyInstaller")
        os.makedirs(pkg)
        io.open(os.path.join(pkg, "__init__.py"), "w",
                encoding="ascii", newline="\n").write("")
        io.open(os.path.join(pkg, "__main__.py"), "w",
                encoding="ascii", newline="\n").write(
            "import os\n"
            "os.makedirs('dist', exist_ok=True)\n"
            "open(os.path.join('dist', 'NetBelt.exe'), 'wb').write(b'exe')\n")

        # 走らせ終えたら、借りたコンソールのコードページを返す
        self.kernel32 = ctypes.windll.kernel32
        original = self.kernel32.GetConsoleOutputCP()
        if not original:
            self.skipTest("コンソールが無く、コードページを固定できない")
        self.addCleanup(self._set_codepage, original)

    def _set_codepage(self, codepage):
        return bool(self.kernel32.SetConsoleCP(codepage)
                    and self.kernel32.SetConsoleOutputCP(codepage))

    def _place_updater(self):
        shutil.copyfile(UPDATER_BAT, os.path.join(self.proj, "updater.bat"))

    def _run(self):
        """CP932 のコンソールで build.bat を走らせ、終了コードと生バイトを返す。"""
        if not self._set_codepage(SHIFT_JIS):
            self.skipTest("この環境では CP932 を使えない")
        out_path = os.path.join(self.base, "out.txt")
        env = dict(os.environ)
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
        return code, io.open(out_path, "rb").read()

    def _stray_lines(self, raw):
        """断片がコマンドとして実行された跡の行を集める。"""
        found = []
        for codec in ("cp932", "utf-8"):
            text = raw.decode(codec, errors="replace")
            for line in text.splitlines():
                if any(mark in line for mark in NOT_RECOGNIZED):
                    found.append(line)
        return found

    def _report(self, stray):
        return ("行の断片がコマンドとして実行されている:\n"
                + "\n".join(s.encode("ascii", "backslashreplace").decode()
                            for s in stray))

    def test_the_failed_copy_path_runs_no_leftover_fragments(self):
        """updater.bat が無い経路で、行の断片が実行されないこと。"""
        code, raw = self._run()

        self.assertTrue(os.path.exists(self.exe),
                        "前提が崩れている: 偽の PyInstaller が exe を作っていない")
        self.assertFalse(
            os.path.exists(os.path.join(self.dist, "updater.bat")),
            "前提が崩れている: updater.bat が dist へ入っている")
        stray = self._stray_lines(raw)
        self.assertEqual(stray, [], self._report(stray))
        self.assertNotEqual(
            code, 0,
            "updater.bat をコピーできなかったのに成功として終わっている")

    def test_the_successful_path_runs_no_leftover_fragments(self):
        """成功する経路でも、行の断片が実行されないこと。"""
        self._place_updater()

        code, raw = self._run()

        stray = self._stray_lines(raw)
        self.assertEqual(stray, [], self._report(stray))
        self.assertEqual(code, 0, "成功したビルドを失敗にしている")


if __name__ == "__main__":
    unittest.main()
