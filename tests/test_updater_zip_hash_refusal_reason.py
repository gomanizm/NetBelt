"""ハッシュが食い違って中止したとき、その理由を伝えていることを見る（テストの穴埋め）。

利用者の決定（2026-09-20 / release-03）: NetBelt が確かめた ZIP の SHA-256 を
updater.bat へ渡し、展開の直前に突き合わせる。食い違ったらインストール先を
変えずに中止し、その旨を伝える。

tests/test_updater_zip_hash_argument.py の
test_a_zip_swapped_after_the_check_is_refused は、終了コードが 0 でないこと・
「更新が完了しました」が無いこと・exe が変わっていないこと・適用用の写しが
消えたことだけを見ていて、中止に至った理由を見ていなかった。

実測（v1.3.2 の再調査 mut_ziphash_reason.py。updater.bat の写しに変異を入れ、
テストモジュールの UPDATER だけをその写しへ向けて流した）:

  - M1: ハッシュ不一致の exit 2 を exit 1 に変える（「ZIPファイルの展開に
    失敗しました」へ落ち、不一致だったことが伝わらない）
  - M2: 不一致の案内の 1 行目（「更新ファイルの中身が、確認した時点から
    変わっています」）を消す

どちらでも既存のテストは pass した。つまり「その旨を伝える」が崩れても
気づけなかった。

埋めた形: 同じ差し替えの手順で走らせ、出力に不一致の案内があり、
「展開に失敗しました」が無いことを確かめる。M1・M2 のどちらでも落ち、
v1.3.1 の updater.bat では通る（実測）。既存のテストは変えていない。
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

MISMATCH = "更新ファイルの中身が、確認した時点から変わっています"


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterZipHashRefusalReasonTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_zipreason_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        with io.open(os.path.join(self.app_dir, "NetBelt.exe"), "wb") as f:
            f.write(b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)

    def _make_zip(self, tag):
        # 実際に渡されるのは stage_for_apply が作る写しなので、同じ名前にする。
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

    def _run(self, zip_path, expected):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s" "%s" "%s" "%s"' % (
                    self.updater, zip_path,
                    os.path.join(self.app_dir, "NetBelt.exe"), expected),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env)
            code = proc.wait(timeout=300)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def test_the_refusal_says_the_hash_did_not_match(self):
        """差し替えられた ZIP を断るとき、ハッシュの不一致を理由として伝えること。"""
        good = self._make_zip("GOOD")
        expected = self._sha256(good)
        evil = self._make_zip("EVIL")
        # 確かめた写しだけが、別の有効な ZIP へ置き換えられた状態にする
        os.replace(evil, good)

        code, text = self._run(good, expected)

        self.assertNotEqual(code, 0, "差し替えられた ZIP を当てた:\n" + text)
        self.assertIn(MISMATCH, text,
                      "中止の理由（ハッシュの不一致）が伝わっていない:\n" + text)
        self.assertNotIn("展開に失敗しました", text,
                         "不一致を展開の失敗として伝えた:\n" + text)
        self.assertNotIn("更新が完了しました", text, text)
        with io.open(os.path.join(self.app_dir, "NetBelt.exe"), "rb") as f:
            self.assertEqual(f.read(), b"OLD-EXE",
                             "インストール先の exe を書き換えた:\n" + text)


if __name__ == "__main__":
    unittest.main()
