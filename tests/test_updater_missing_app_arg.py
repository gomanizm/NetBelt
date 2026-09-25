"""updater.bat が引数不足で中止したときも、適用用の写しを残さないことを検証する。

NetBelt は更新を当てる直前に、検証した ZIP の写し（更新フォルダの
NetBelt-apply-*.zip）と控え（.sha256 / .version）を作り、写しのパスを
updater.bat へ渡す。インストール先へ何も書かずに中止する道は、その写しを
控えごと消してから止まる（tests/test_updater_apply_copy_cleanup.py）。

実測（検査役 cx5g-verify-release の p3_nocopy_and_args.py、ac1dee7 相当）:
再入後の引数チェックだけが、その扱いから漏れていた。アプリケーションパス
（第2引数）を渡さずに updater.bat を起動すると、『エラー: アプリケーション
パスが指定されていません』・exit 1 で、インストール先へは何も書かないまま
止まるのに、更新フォルダには NetBelt-apply-k3j9x2.zip と控え 2 つがそのまま
残った。どれも検証記録つきの .zip なので、未適用の更新の候補にも並ぶ。
元の ZIP は更新フォルダに残っているので、写しは当て直しの材料にもならない。
NetBelt 本体は第2引数に必ず sys.executable を渡すのでこの道へは届かないが、
漏れの種類は 4 周目に塞いだ他の中止経路と同じ。

直し方: ZIP_FILE を第2引数の検査より前に決めて、この中止からも
:drop_apply_copy を呼べるようにした。ZIP のパスそのものが渡されていない
第1引数の中止は、消すべき写しの在り処が分からないので、これまでどおり何も
消さない。手で渡した ZIP を消さないのも、これまでどおり。
"""
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

VERSION = "9.9.9"
APPLY_NAME = "NetBelt-apply-k3j9x2.zip"
ORIGINAL_NAME = "NetBelt-%s.zip" % VERSION


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("NetBelt.exe", b"NEW-EXE")
    return buf.getvalue()


class UpdaterMissingAppArgTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_updarg_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.upd = os.path.join(self.base, "NetBeltUpdates")
        os.makedirs(self.upd)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(b"OLD-EXE")
        self.body = _zip_bytes()
        # 元の ZIP。どの中止でも手を付けてはいけない
        self._put_set(ORIGINAL_NAME)

    def _put_set(self, name):
        """ZIP と控え（.sha256 / .version）の組を更新フォルダへ置く。"""
        path = os.path.join(self.upd, name)
        with io.open(path, "wb") as f:
            f.write(self.body)
        with io.open(path + ".sha256", "w", encoding="ascii") as f:
            f.write(hashlib.sha256(self.body).hexdigest())
        with io.open(path + ".version", "w", encoding="ascii") as f:
            f.write(VERSION)
        return path

    def _run(self, args):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            code = subprocess.Popen(
                '"%s" %s' % (self.updater, args), stdout=out,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env).wait(timeout=180)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def _names(self):
        return sorted(os.listdir(self.upd))

    def _original_set(self):
        return [ORIGINAL_NAME, ORIGINAL_NAME + ".sha256",
                ORIGINAL_NAME + ".version"]

    def _assert_aborted_untouched(self, code, out):
        self.assertNotEqual(code, 0, "中止したのに成功と報告した:\n" + out)
        with io.open(self.app_path, "rb") as f:
            self.assertEqual(f.read(), b"OLD-EXE",
                             "中止したのにインストール先を書き換えた:\n" + out)

    def test_a_missing_app_path_argument_drops_the_apply_copy(self):
        """第2引数が無くて中止したとき、写しを控えごと消すこと。"""
        copy = self._put_set(APPLY_NAME)

        code, out = self._run('"%s"' % copy)

        self._assert_aborted_untouched(code, out)
        self.assertIn("アプリケーションパスが指定されていません", out, out)
        self.assertEqual(self._names(), self._original_set(),
                         "適用用の写しが残ったか、元の ZIP まで消した:\n" + out)

    def test_a_missing_app_path_argument_keeps_a_hand_made_zip(self):
        """写しでない ZIP は、第2引数が無くて中止しても消さないこと。"""
        code, out = self._run('"%s"' % os.path.join(self.upd, ORIGINAL_NAME))

        self._assert_aborted_untouched(code, out)
        self.assertIn("アプリケーションパスが指定されていません", out, out)
        self.assertEqual(self._names(), self._original_set(),
                         "手で渡した ZIP を消した:\n" + out)

    def test_a_missing_zip_argument_leaves_the_update_folder_alone(self):
        """第1引数が無い中止は、消すべき写しが分からないので何も消さないこと。"""
        self._put_set(APPLY_NAME)
        expected = self._names()

        code, out = self._run('"" ""')

        self._assert_aborted_untouched(code, out)
        self.assertIn("ZIPファイルパスが指定されていません", out, out)
        self.assertEqual(self._names(), expected,
                         "在り処が分からないはずの写しに手を付けた:\n" + out)


if __name__ == "__main__":
    unittest.main()
