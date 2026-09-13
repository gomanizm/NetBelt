"""updater.bat が、自分の作ったもの以外の backup_* を消さないことを検証する。

「7日以上前のバックアップを掃除する」つもりのループが、次の形だった。

    for /d %%d in ("!APP_DIR!backup_*") do (
        forfiles /p "%%d" /d -7 >nul 2>&1
        if not errorlevel 1 rd /s /q "%%d"
    )

forfiles /d -7 は「7日以上前のエントリが1件でもある」と errorlevel 0 を
返すだけで、その後の rd /s /q はディレクトリ全体を再帰削除する。実測:
今日のファイルを含む backup_router も丸ごと消え、backup_only_old も
消えた（inspector, release #3）。

NetBelt 自身は backup_* を作らない。ポータブル配布なので、利用者が
機器コンフィグの退避先として backup_* を隣に置いている可能性は否定
できず、その中身は取り返しがつかない。掃除するなら、スクリプトが
付けた識別子（backup_netbelt_*）に限り、ディレクトリ自身の日付で
判断すること。
"""
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

EIGHT_DAYS = 8 * 24 * 3600


class UpdaterBackupDirsTest(unittest.TestCase):
    """tests/test_updater_script.py と同じ隔離された実行環境で走らせる。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_bak_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self.app_path = os.path.join(self.app_dir, "dummy_app.exe")
        shutil.copyfile(
            os.path.join(os.environ["SystemRoot"], "System32",
                         "rundll32.exe"), self.app_path)
        io.open(os.path.join(self.app_dir, "NetBelt.exe"), "w",
                encoding="ascii").write("old")

    def _dir(self, name, files, age=0):
        """files: {ファイル名: 経過秒}。age はディレクトリ自身の経過秒。"""
        path = os.path.join(self.app_dir, name)
        os.makedirs(path)
        now = time.time()
        for fname, file_age in files.items():
            fpath = os.path.join(path, fname)
            io.open(fpath, "w", encoding="ascii").write("x")
            os.utime(fpath, (now - file_age, now - file_age))
        os.utime(path, (now - age, now - age))
        return path

    def _run(self):
        zip_path = os.path.join(self.base, "update.zip")
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("NetBelt.exe", "new")
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s" "%s" "%s"' % (self.updater, zip_path, self.app_path),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
            code = proc.wait(timeout=180)
        return code, io.open(out_path, encoding="utf-8",
                             errors="replace").read()

    def test_a_users_backup_folder_with_one_old_file_survives(self):
        """古いファイルが混ざっているだけの backup_* を丸ごと消さないこと。"""
        keep = self._dir("backup_router",
                         {"router_8days.cfg": EIGHT_DAYS,
                          "router_today.cfg": 0})

        code, out = self._run()

        self.assertEqual(code, 0, out)
        self.assertTrue(
            os.path.exists(os.path.join(keep, "router_today.cfg")),
            "利用者の backup_router を消している:\n" + out)
        self.assertTrue(
            os.path.exists(os.path.join(keep, "router_8days.cfg")),
            "利用者の backup_router の中身を消している:\n" + out)

    def test_a_users_backup_folder_that_is_all_old_survives(self):
        """全部古くても、スクリプトが作ったものでなければ触らないこと。"""
        keep = self._dir("backup_only_old", {"x.cfg": EIGHT_DAYS},
                         age=EIGHT_DAYS)

        code, out = self._run()

        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(keep),
                        "利用者の backup_only_old を消している:\n" + out)

    def test_a_backup_named_like_the_script_survives_too(self):
        """backup_netbelt_* も消さないこと。

        この名前のフォルダを作る実装は NetBelt のどこにも無い。つまり
        その名前が実際にあるなら利用者が付けたものであり、日付がいくら
        古くても中身は取り返しがつかない。
        """
        old = self._dir("backup_netbelt_20200101", {"NetBelt.exe": EIGHT_DAYS},
                        age=EIGHT_DAYS)
        recent = self._dir("backup_netbelt_today", {"NetBelt.exe": 0})

        code, out = self._run()

        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(old, "NetBelt.exe")),
                        "古い backup_netbelt_* を消している:\n" + out)
        self.assertTrue(os.path.isdir(recent),
                        "新しい backup_netbelt_* を消している:\n" + out)


if __name__ == "__main__":
    unittest.main()
