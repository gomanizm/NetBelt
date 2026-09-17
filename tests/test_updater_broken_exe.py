"""起動できない exe に差し替わったとき、updater.bat が黙らないことを検証する。

以前は start の戻り値を一切見ずに「起動しました」と表示し、続けて
ZIP と .sha256 / .version を消していた。実測では、Windows 自身が
「このバージョンの ... は互換性がありません」と書いた直後の行で
「起動しました」が出て、再試行の材料も消えていた。利用者から見ると、
壊れたことにも、やり直す材料が無くなったことにも気づけない。

start は成功しても errorlevel を 0 に戻さないので、そのまま
`if errorlevel 1` を書くと直前の失敗を start の失敗として読む
（v1.1.0 の不具合。tests/test_updater_script.py を参照）。実測では

    直前を 0 に均してから 起動できない exe を start  -> 216
    直前を 9 にしてから  起動できる   exe を start  -> 9
    直前を 0 に均してから 起動できる   exe を start  -> 0

なので、直前で 0 に均しておけば、持ち越しの誤判定と本当の起動失敗の
検出は両立する。

残る制限: 更新そのものは当たっているので、終了コードと
「更新が完了しました！」の表示は変えていない。ここで見るのは
「起動したと嘘をつかないこと」と「再試行の材料を消さないこと」。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")
RUNDLL32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", "rundll32.exe")


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterBrokenExeTest(unittest.TestCase):
    """tests/test_updater_exe_staging.py と同じ隔離された実行環境で走らせる。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_broken_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        # 更新前は本当に起動できる exe が置いてある状態にする。
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        shutil.copyfile(RUNDLL32, self.app_path)
        self.zip_path = os.path.join(self.base, "update.zip")

    def _run(self, exe_body):
        """exe_body を NetBelt.exe として持つ ZIP を、サイドカー付きで当てる。"""
        with zipfile.ZipFile(self.zip_path, "w") as z:
            z.writestr("NetBelt.exe", exe_body)
        # 実アプリは ZIP の隣に検証用のサイドカーを置く
        io.open(self.zip_path + ".sha256", "w",
                encoding="ascii", newline="").write("0" * 64)
        io.open(self.zip_path + ".version", "w",
                encoding="ascii", newline="").write("9.9.9")
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s" "%s" "%s"' % (self.updater, self.zip_path,
                                    self.app_path),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
            code = proc.wait(timeout=180)
        return code, io.open(out_path, encoding="utf-8",
                             errors="replace").read()

    def _sidecars(self):
        return {name: os.path.exists(self.zip_path + suffix)
                for name, suffix in (("zip", ""), ("sha256", ".sha256"),
                                     ("version", ".version"))}

    def test_an_exe_that_cannot_start_is_not_reported_as_started(self):
        """起動できない exe に差し替わったら「起動しました」と言わないこと。"""
        code, out = self._run("new")

        self.assertNotIn("起動しました", out,
                         "起動できていないのに起動したと表示している:\n" + out)
        self.assertIn("起動できませんでした", out,
                      "起動できなかったことを伝えていない:\n" + out)

    def test_an_exe_that_cannot_start_keeps_the_zip_for_a_retry(self):
        """起動に失敗したら、やり直す材料を消さないこと。

        ZIP と .sha256 / .version が消えると、利用者にはもう一度
        当て直す手立てが残らない。
        """
        code, out = self._run("new")

        self.assertEqual(
            self._sidecars(),
            {"zip": True, "sha256": True, "version": True},
            "起動に失敗したのに再試行の材料を消している:\n" + out)

    def test_a_working_exe_is_still_reported_as_started(self):
        """起動できた場合は、これまでどおり報告して後片付けすること。"""
        code, out = self._run(io.open(RUNDLL32, "rb").read())

        self.assertEqual(code, 0, out)
        self.assertIn("起動しました", out,
                      "起動できているのに報告していない:\n" + out)
        self.assertNotIn("起動できませんでした", out, out)
        self.assertEqual(
            self._sidecars(),
            {"zip": False, "sha256": False, "version": False},
            "成功したのに後片付けしていない:\n" + out)


if __name__ == "__main__":
    unittest.main()
