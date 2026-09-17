"""改名した exe から更新したとき、updater.bat が当てたふりをしないことを検証する。

NetBelt.exe を別名（例: MyTool.exe）へ改名して使っている場合、
updater.bat は展開した新しい exe を !APP_DIR!NetBelt.exe として据える
だけで、動いている MyTool.exe には一切触れない。それでも
「更新が完了しました！」と表示し、終了コード 0 を返し、ZIP まで消す。
利用者から見ると、身に覚えのない NetBelt.exe が増えただけで版は上がらず、
同じ更新が何度でも通知され続ける。

ここでは「改名された exe を渡されたら、何も書き換えずに理由を述べて
止まる」ことを判定する。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")


@unittest.skipUnless(os.name == "nt", "cmd.exe が要る")
class UpdaterRenamedExeTest(unittest.TestCase):
    """tests/test_updater_exe_staging.py と同じ隔離された実行環境で走らせる。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_rename_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        # 利用者が NetBelt.exe を改名して使っている状態。起動し直される
        # ので、中身は実在の exe にしておく。
        self.app_path = os.path.join(self.app_dir, "MyTool.exe")
        shutil.copyfile(
            os.path.join(os.environ["SystemRoot"], "System32",
                         "rundll32.exe"), self.app_path)
        self.app_bytes = io.open(self.app_path, "rb").read()

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
        return code, zip_path, io.open(out_path, encoding="utf-8",
                                       errors="replace").read()

    def test_a_renamed_exe_is_refused_instead_of_reported_as_updated(self):
        """改名された exe を渡されたら、成功と報告しないこと。"""
        code, zip_path, out = self._run({"NetBelt.exe": "new",
                                         "README.txt": "r"})

        self.assertNotEqual(
            code, 0,
            "動いている exe を更新していないのに成功として終わっている:\n" + out)
        self.assertIn("MyTool.exe", out,
                      "どの実行ファイルが対象外なのかを伝えていない:\n" + out)
        self.assertEqual(
            io.open(self.app_path, "rb").read(), self.app_bytes,
            "中止したのに動いている exe を書き換えている:\n" + out)
        self.assertFalse(
            os.path.exists(os.path.join(self.app_dir, "NetBelt.exe")),
            "中止したのに別名の NetBelt.exe を新設している:\n" + out)
        self.assertFalse(
            os.path.exists(os.path.join(self.app_dir, "README.txt")),
            "中止したのに同梱ファイルを展開先へ置いている:\n" + out)
        self.assertTrue(
            os.path.exists(zip_path),
            "当てていない更新の ZIP を消している:\n" + out)


if __name__ == "__main__":
    unittest.main()
