"""更新が正常に当たったのに「The system cannot find the path specified.」が出ていた件の回帰。

実測（v1.3.1 = 441ea02 の updater.bat をそのまま 1 回走らせた）: rc=0・
「更新が完了しました！」で終わるのに、「クリーンアップ中...」の直後に

    The system cannot find the path specified.

が 1 行出ていた。出どころは :release_lock_sweep の 2 周目。1 周目の
rd /s /q で自分の目印（NetBelt-update-lock）を消した後、2 周目の

    set /p LOCK_OWNER=<"!LOCK_DIR!\\holder.txt" 2>nul

が、もう無いフォルダの中のファイルを入力として開こうとして失敗する。
この失敗の文言はリダイレクトを組み立てる段階で出るので、同じコマンドに
付けた 2>nul がまだ効いておらず、そのまま画面に出ていた（chcp 65001 の
下では英語、コードページを変えない cmd では「指定されたパスが見つかり
ません。」）。更新は成功しているのに、エラーのような 1 行が毎回残る。

直した形: その 1 行を (set /p LOCK_OWNER=<"...") 2>nul と括弧で包み、
リダイレクトの失敗ごと 2>nul で黙らせる。ファイルがあるときは、これまで
どおり 1 行目を読める（2 周走査する仕組みそのものは変えない）。
"""
import hashlib
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

# chcp 65001 の下では英語で出る。念のため日本語の文言も見る。
STRAY_MESSAGES = ("The system cannot find the path specified",
                  "指定されたパスが見つかりません")


def _runnable_exe():
    """据える新しい exe。起動できる本物にして、正常な道を最後まで通す。"""
    path = os.path.join(os.environ["SystemRoot"], "System32", "rundll32.exe")
    with io.open(path, "rb") as f:
        return f.read()


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterReleaseLockQuietTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_relquiet_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.new_exe = _runnable_exe()
        self.zip_path = os.path.join(self.base, "NetBelt-apply-test.zip")
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", self.new_exe)
            z.writestr("README.txt", "NEW-README")

    def _sha256(self):
        digest = hashlib.sha256()
        with io.open(self.zip_path, "rb") as f:
            digest.update(f.read())
        return digest.hexdigest()

    def _run(self):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s" "%s" "%s" "%s"' % (self.updater, self.zip_path,
                                         self.app_path, self._sha256()),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env)
            code = proc.wait(timeout=300)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def test_a_successful_update_prints_no_missing_path_error(self):
        """正常に当たった更新の出力に、パスが見つからないという行が出ないこと。"""
        code, text = self._run()

        # 前提: 起動まで含めて正常な道を通ったこと
        self.assertEqual(code, 0, text)
        self.assertIn("更新が完了しました", text, text)
        self.assertIn("起動しました", text, text)
        with io.open(self.app_path, "rb") as f:
            self.assertEqual(f.read(), self.new_exe, text)
        self.assertEqual(
            sorted(n for n in os.listdir(self.app_dir)
                   if n.startswith("NetBelt-update-")), [],
            "目印が残った:\n" + text)

        stray = [line for line in text.splitlines()
                 if any(m in line for m in STRAY_MESSAGES)]
        self.assertEqual(stray, [],
                         "成功した更新なのにエラーのような行が出た:\n" + text)


if __name__ == "__main__":
    unittest.main()
