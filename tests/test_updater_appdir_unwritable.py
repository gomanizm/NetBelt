"""インストール先へ書けないときに、updater.bat が理由を取り違えないことを検証する。

updater.bat は [5/6] の手前で、インストール先に目印フォルダ
（NetBelt-update-lock）を md で作って排他を取る。md は「同じ名前が既にある」
ときに失敗するので、それが重なりの検出になっている。ところが md は
「そのフォルダへ書けない」ときにも同じように失敗する。両方を重なりとして
扱っていたため、書けない場所（Program Files 配下など）へ NetBelt を置いた
利用者には、待っても直らないものを待たせていた。

実測（8d316a7、この砂場と同じ小さな作り物、icacls で書き込みを拒否）:

    [5/6] ファイルを更新中...
      コピー元: ...
      コピー先: ...
    エラー: 別の更新が進行中です
      同じインストール先への更新が既に動いています。インストール先の
      ファイルは何も変えていません。先の更新が終わるのを待ってから、
      もう一度お試しください。

exit 1。実際には他の更新はどこにも動いていない。

直し方: md が失敗したら、誰とも重ならない名前（NetBelt-update-probe.<目印>）
でもう一度作ってみる。それも作れなければ、重なりではなくインストール先へ
書けないほうだと分かるので、そのまま伝えて中止する。作れたときは片付けて、
これまでどおり重なりとして扱う。

ここでは実物の配布物は使わない。数百バイトの exe 代用と zip で同じ道筋を
通し、インストール先の書き込みを icacls の拒否で止める。
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


class UpdaterAppDirUnwritableTest(unittest.TestCase):
    """書けないインストール先では、書けないことを伝えて止まること。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_nowrite_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        shutil.copyfile(UPDATER, os.path.join(self.app_dir, "updater.bat"))
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(b"OLD-EXE")

        # NetBelt が適用の直前に作る写しと同じ名前・同じ控え付きで置く。
        self.zip_path = os.path.join(self.base, "NetBelt-apply-test.zip")
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", b"NEW-EXE")
            z.writestr("zz_bundled.txt", b"new-bundled")
        for suffix, body in ((".sha256", "0" * 64), (".version", "9.9.9")):
            with io.open(self.zip_path + suffix, "w", encoding="ascii") as f:
                f.write(body)

    def _deny_writes(self):
        """インストール先への書き込みを止める（後始末で必ず戻す）。"""
        user = os.environ["USERNAME"]
        subprocess.run(
            ["icacls", self.app_dir, "/deny",
             "%s:(OI)(CI)(WD,AD,WEA,WA,DC,DE)" % user],
            capture_output=True, text=True)
        self.addCleanup(subprocess.run,
                        ["icacls", self.app_dir, "/remove:d", user],
                        capture_output=True, text=True)
        # 前提の確認。拒否が効いていなければ、この試験は何も測れていない。
        # その環境で落としても直しようが無く、リリースの門番（CI の pytest）を
        # 塞ぐだけなので、失敗ではなく見送りにして理由を残す。
        probe = os.path.join(self.app_dir, "probe")
        try:
            os.mkdir(probe)
        except OSError:
            return
        os.rmdir(probe)
        self.skipTest("インストール先への書き込みを止められない環境（icacls の"
                      "拒否が効かない）のため、この道筋は測れない")

    def _run(self):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s" "%s" "%s"' % (os.path.join(self.app_dir, "updater.bat"),
                                    self.zip_path, self.app_path),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env)
            code = proc.wait(timeout=180)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def test_the_reason_given_is_the_write_permission(self):
        """書けないことが理由なら、そう伝えること（重なりのせいにしない）。"""
        self._deny_writes()

        code, out = self._run()

        self.assertNotEqual(code, 0, "当てていないのに成功として終わった:\n" + out)
        self.assertNotIn("更新が完了しました", out, out)
        self.assertIn("インストール先へ書き込めません", out, out)
        self.assertNotIn("別の更新が進行中", out,
                         "書けないだけなのに、重なりのせいにしている:\n" + out)

    def test_nothing_in_the_install_folder_is_touched(self):
        """中止したときは、インストール先を何も変えないこと。"""
        self._deny_writes()

        code, out = self._run()

        with io.open(self.app_path, "rb") as f:
            self.assertEqual(f.read(), b"OLD-EXE", out)
        self.assertEqual(sorted(os.listdir(self.app_dir)),
                         ["NetBelt.exe", "updater.bat"],
                         "インストール先に何か置いた:\n" + out)

    def test_a_writable_folder_still_updates(self):
        """書ける場所では、これまでどおり当たること（取り違えの逆を防ぐ）。"""
        code, out = self._run()

        self.assertEqual(code, 0, out)
        self.assertIn("更新が完了しました", out, out)
        with io.open(self.app_path, "rb") as f:
            self.assertEqual(f.read(), b"NEW-EXE", out)


if __name__ == "__main__":
    unittest.main()
