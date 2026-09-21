"""updater.bat が、展開の直前に受け取った期待ハッシュと突き合わせることの回帰。

NetBelt は適用の直前に、検証した ZIP の写し（更新フォルダの
NetBelt-apply-*.zip）を作って渡す（version_manager.py の stage_for_apply）。
写しの置き場は元と同じ更新フォルダで、そこへ書ける相手はフォルダを列挙すれば
写しの名前も知れる、という窓が残っていた。

実測（検査役 cx5j-check-release の p5_staged_swap.py、16101ef）:
検証済みの組から stage_for_apply が写しを作り（再検証も通る）、その写しだけを
別の有効な ZIP（中の NetBelt.exe が EXE_FROM_EVIL）へ os.replace で置き換えて
から、その写しのパスを updater.bat へ渡すと

    is_verified_update(staged) = False（＝控えと食い違っている）
    updater rc = 0、『更新が完了しました』= True
    据わった exe = EXE_FROM_EVIL

updater.bat は渡されたパスを Expand-Archive で開くだけで、ハッシュを計算する
箇所がどこにも無かった（Get-FileHash も sha256 の照合も 1 つも無く、.sha256 は
del の対象としてしか出てこない）。tests/test_update_apply_staging.py が差し替えて
いたのは元の ZIP だけで、写しそのものの差し替えは見ていなかった。

利用者の決定（2026-09-20 / release-03）: ハッシュを渡す。updater.bat の引数を
1 つ増やして期待ハッシュを渡し、Expand-Archive の前に Get-FileHash で
突き合わせる。食い違ったらインストール先を変えずに中止し、その旨を伝える。
効き始めるのは次の版を当てた後なので、古い updater を呼ぶ場合に引数が増えても
壊れないこと。

前提: 更新フォルダ（%TEMP%\\NetBeltUpdates）へ書ける相手＝同じ利用者の権限で
既にコードを実行できている相手なので、これは権限昇格の話ではない。
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
from unittest import mock

sys.path.insert(0, "src")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

# 再入のときに期待ハッシュを子へ渡している部分。これを抜くと、引数が 1 つ
# 増えたことを知らない（＝この直しより前の）updater.bat と同じ振る舞いになる。
HANDOVER = b' "!A3!"'


def _updater_that_ignores_the_extra_argument():
    """%3 を受け取っても何もしない、古い updater.bat 相当を返す。"""
    data = io.open(UPDATER, "rb").read()
    assert data.count(HANDOVER) == 1, "再入の引数の渡し方が変わっている"
    return data.replace(HANDOVER, b"")


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterZipHashArgumentTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_ziphash_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self._write("NetBelt.exe", b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)

    def _write(self, name, body):
        with io.open(os.path.join(self.app_dir, name), "wb") as f:
            f.write(body)

    def _read(self, name):
        with io.open(os.path.join(self.app_dir, name), "rb") as f:
            return f.read()

    def _make_zip(self, tag):
        # 実際に渡されるのは stage_for_apply が作る写しなので、
        # 中止したときの後始末（:drop_apply_copy）まで同じ名前で見る。
        path = os.path.join(self.base, "NetBelt-apply-%s.zip" % tag)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", "EXE_FROM_%s" % tag)
        return path

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with io.open(path, "rb") as f:
            digest.update(f.read())
        return digest.hexdigest()

    def _run(self, zip_path, expected=None, tag="A"):
        out_path = os.path.join(self.base, "%s.txt" % tag)
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        command = '"%s" "%s" "%s"' % (
            self.updater, zip_path, os.path.join(self.app_dir, "NetBelt.exe"))
        if expected is not None:
            command += ' "%s"' % expected
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(command, stdout=out,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env)
            code = proc.wait(timeout=300)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def test_a_zip_swapped_after_the_check_is_refused(self):
        """渡された期待ハッシュと違う中身は、展開せずに中止すること。"""
        good = self._make_zip("GOOD")
        expected = self._sha256(good)
        evil = self._make_zip("EVIL")
        # 確かめた写しだけが、別の有効な ZIP へ置き換えられた状態にする
        os.replace(evil, good)

        code, text = self._run(good, expected)

        self.assertNotEqual(code, 0, "差し替えられた ZIP を当てた:\n" + text)
        self.assertNotIn("更新が完了しました", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"OLD-EXE",
                         "インストール先の exe を書き換えた:\n" + text)
        self.assertFalse(os.path.exists(good),
                         "中止したのに適用用の写しが残った:\n" + text)

    def test_a_matching_hash_still_applies_the_update(self):
        """期待ハッシュと一致していれば、これまでどおり当たること。"""
        good = self._make_zip("GOOD")

        code, text = self._run(good, self._sha256(good))

        self.assertEqual(code, 0, text)
        self.assertIn("更新が完了しました", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_GOOD", text)

    def test_the_update_still_applies_without_a_hash(self):
        """期待ハッシュを渡さない呼び出し（古い NetBelt・手動）も通ること。"""
        good = self._make_zip("GOOD")

        code, text = self._run(good)

        self.assertEqual(code, 0, text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_GOOD", text)

    def test_an_updater_that_does_not_know_the_hash_ignores_it(self):
        """引数が 1 つ増えても、それを知らない updater.bat は壊れないこと。"""
        with io.open(self.updater, "wb") as f:
            f.write(_updater_that_ignores_the_extra_argument())
        good = self._make_zip("GOOD")

        code, text = self._run(good, self._sha256(good))

        self.assertEqual(code, 0, text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_GOOD", text)


class LaunchUpdaterHashTest(unittest.TestCase):
    """NetBelt 側が、控えたハッシュを updater.bat へ渡すこと。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-ziphash-py-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _manager(self):
        from core.version_manager import VersionManager
        return VersionManager()

    def _staged(self):
        body = b"PK\x03\x04 netbelt update"
        zip_path = os.path.join(self.dir, "NetBelt-9.9.9.zip")
        io.open(zip_path, "wb").write(body)
        io.open(zip_path + ".sha256", "w", encoding="ascii").write(
            hashlib.sha256(body).hexdigest())
        io.open(zip_path + ".version", "w", encoding="ascii").write("9.9.9")
        staged, problem = self._manager().stage_for_apply(zip_path, "9.9.9")
        self.assertIsNone(problem)
        return staged, hashlib.sha256(body).hexdigest()

    def test_the_recorded_hash_is_passed_as_a_fourth_argument(self):
        staged, expected = self._staged()
        with mock.patch("core.version_manager.subprocess.Popen") as popen:
            self._manager().launch_updater(
                "C:\\app\\updater.bat", staged, "C:\\app\\NetBelt.exe")
        command = popen.call_args[0][0]
        self.assertEqual(command.split('" "')[3].rstrip('"'), expected,
                         "期待ハッシュが渡っていない: %s" % command)

    def test_nothing_is_passed_when_the_record_is_missing(self):
        """控えが無い呼び出しでは、引数を増やさないこと。"""
        staged, _ = self._staged()
        os.remove(staged + ".sha256")
        with mock.patch("core.version_manager.subprocess.Popen") as popen:
            self._manager().launch_updater(
                "C:\\app\\updater.bat", staged, "C:\\app\\NetBelt.exe")
        self.assertEqual(popen.call_args[0][0].count('" "'), 2,
                         popen.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
