"""TEMP のパスに ! があるときも、理由を言って止まることを検証する。

updater.bat は `!` を含むパスを遅延展開が食べてしまうため、更新できない
環境では ASCII の "exclamation mark" の説明を出して止まる。ところが
その検査は自分のパスと 2 つの引数しか見ておらず、TEMP は見ていない。
実行は TEMP への写しから行うので、`%TEMP%` に `!` があると写し先の
パスが崩れ、コピーに失敗して :nocopy の「TEMP の空きを確保せよ」という
見当違いの案内で終わる。

実アプリでは ZIP も TEMP 下に置くので updater_command が先に
ValueError で止め、この bat の経路には届かない。bat を直接使う場合や、
Python 側の tempfile と cmd の %TEMP% が別を指す環境で踏む。
案内が間違っているのは直す価値がある。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, "src")

UPDATER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "updater.bat")


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterTempBangTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt-upd-temp-")
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        # 再起動先は NetBelt.exe。updater.bat は改名された exe を
        # 受け取ると更新を当てずに中止するため、本番と同じ名前で渡す。
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        io.open(self.app_path, "w",
                encoding="ascii", newline="").write("old")
        self.zip_path = os.path.join(self.base, "update.zip")
        with zipfile.ZipFile(self.zip_path, "w") as z:
            z.writestr("NetBelt.exe", "new")
        # ! を含む TEMP
        self.temp = os.path.join(self.base, "te!mp")
        os.makedirs(self.temp)

    def _run(self):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            command = '"{}" "{}" "{}"'.format(self.updater, self.zip_path,
                                              self.app_path)
            proc = subprocess.Popen(command, stdout=out,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env)
            code = proc.wait(timeout=180)
        return code, io.open(out_path, encoding="utf-8", errors="replace").read()

    def test_a_temp_path_with_an_exclamation_mark_says_why(self):
        code, out = self._run()

        self.assertNotEqual(code, 0, "失敗として返していない\n" + out)
        self.assertIn("exclamation mark", out,
                      "理由を言っていない:\n" + out)
        self.assertNotIn("could not copy the updater to TEMP", out,
                         "見当違いの案内（TEMP の空き）を出している:\n" + out)

    def test_nothing_is_changed(self):
        self._run()
        self.assertEqual(
            io.open(os.path.join(self.app_dir, "NetBelt.exe"),
                    encoding="ascii").read(), "old",
            "中途半端に書き換えている")


if __name__ == "__main__":
    unittest.main()
