"""インストール先にある NetBelt-update-lock が更新の目印でないときの扱いの回帰。

実測（検査役 cx5j-check-release の p1_stale_lock_content.py、16101ef）:
updater.bat は目印の回収を「更新日時が10分より古いか」だけで決めており、
holder.txt の有無も中身の有無も見ていなかった。

  * フォルダの場合: インストール先へ NetBelt-update-lock\\memo.txt と
    NetBelt-update-lock\\switch-config\\core1.cfg を置き、全部の更新日時を
    40 分前にして更新を実行 → 出力は『前の更新が残した目印を取り除きました』
    の 1 行だけ、exit=0、memo.txt も core1.cfg も目印フォルダごと消えた。
    更新そのものは完走するので、利用者にはファイルを消したことが伝わらない。
  * ファイルの場合: 同じ名前のファイル（中身あり・40 分前）を置くと md が
    失敗し、Get-Item -Force がそのファイルを拾い、Remove-Item が削除して
    そのまま更新を続けた。利用者のファイルは消えた。

旧 backup_netbelt_* の削除を取りやめた方針（updater.bat の [5/6] の手前に
ある『更新のついでに無断で消してよいものは一つも無い』）とも食い違う。

利用者の決定（2026-09-20 / release-01）: 消さずに中止する。インストール先の
NetBelt-update-lock が更新の作った目印に見えない（ファイル、または holder.txt の
無い中身つきフォルダ）ときは、消さずに更新を中止し、『NetBelt-update-lock という
名前のものがありますが、更新が作った目印ではないようです。中身を確かめて名前を
変えるか移動してください』と伝える（『別の更新が進行中です』とは別の文言）。

直し方: :claim_lock の md が失敗したとき、古さを見るより先に :lock_is_foreign で
中身を見る。ファイル、または holder.txt の無い中身つきフォルダなら :lock_foreign へ
抜け、インストール先へ何も書かずに中止する。空のフォルダ（md の直後で止まった
更新の目印）と holder.txt のあるフォルダは、これまでどおり古さで判断する。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

LOCK_NAME = "NetBelt-update-lock"
FOREIGN_MESSAGE = "更新が作った目印ではないようです"


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterForeignLockNameTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_foreignlock_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        shutil.copyfile(UPDATER, os.path.join(self.app_dir, "updater.bat"))
        self._write("NetBelt.exe", b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.lock = os.path.join(self.app_dir, LOCK_NAME)

    def _write(self, name, body):
        path = os.path.join(self.app_dir, name)
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with io.open(path, "wb") as f:
            f.write(body)

    def _read(self, name):
        with io.open(os.path.join(self.app_dir, name), "rb") as f:
            return f.read()

    def _make_zip(self, tag):
        path = os.path.join(self.base, "%s.zip" % tag)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", "EXE_FROM_%s" % tag)
        return path

    def _age(self, path, minutes=40):
        old = time.time() - minutes * 60
        os.utime(path, (old, old))

    def _run(self, tag="A"):
        out_path = os.path.join(self.base, "%s.txt" % tag)
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s" "%s" "%s"' % (os.path.join(self.app_dir, "updater.bat"),
                                    self._make_zip(tag),
                                    os.path.join(self.app_dir, "NetBelt.exe")),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env)
            code = proc.wait(timeout=300)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def _assert_it_stopped_without_touching_anything(self, code, text):
        self.assertNotEqual(code, 0, "そのまま更新を続けた:\n" + text)
        self.assertIn(FOREIGN_MESSAGE, text, text)
        self.assertNotIn("別の更新が進行中です", text,
                         "動いている更新と同じ文言になっている:\n" + text)
        self.assertNotIn("取り除きました", text, text)
        self.assertNotIn("更新が完了しました", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"OLD-EXE",
                         "インストール先の exe を書き換えた:\n" + text)

    def test_a_file_with_the_marker_name_is_not_deleted(self):
        """同じ名前のファイルは、消さずに中止すること。"""
        with io.open(self.lock, "wb") as f:
            f.write(b"MEMO")
        self._age(self.lock)

        code, text = self._run()

        self._assert_it_stopped_without_touching_anything(code, text)
        self.assertTrue(os.path.isfile(self.lock),
                        "利用者のファイルを消した:\n" + text)
        with io.open(self.lock, "rb") as f:
            self.assertEqual(f.read(), b"MEMO", "中身を書き換えた:\n" + text)

    def test_a_folder_with_contents_but_no_holder_is_not_deleted(self):
        """holder.txt の無い中身つきフォルダは、消さずに中止すること。"""
        os.makedirs(os.path.join(self.lock, "switch-config"))
        with io.open(os.path.join(self.lock, "memo.txt"), "wb") as f:
            f.write(b"MEMO")
        cfg = os.path.join(self.lock, "switch-config", "core1.cfg")
        with io.open(cfg, "wb") as f:
            f.write(b"hostname core1")
        for path in (cfg, os.path.join(self.lock, "switch-config"),
                     os.path.join(self.lock, "memo.txt"), self.lock):
            self._age(path)

        code, text = self._run()

        self._assert_it_stopped_without_touching_anything(code, text)
        self.assertTrue(os.path.isfile(cfg), "利用者のファイルを消した:\n" + text)
        with io.open(cfg, "rb") as f:
            self.assertEqual(f.read(), b"hostname core1",
                             "中身を書き換えた:\n" + text)

    def test_a_fresh_foreign_name_gets_the_same_message(self):
        """更新日時が新しくても、目印でないものは専用の文言で伝えること。"""
        os.makedirs(self.lock)
        with io.open(os.path.join(self.lock, "memo.txt"), "wb") as f:
            f.write(b"MEMO")

        code, text = self._run()

        self._assert_it_stopped_without_touching_anything(code, text)
        self.assertTrue(os.path.isfile(os.path.join(self.lock, "memo.txt")),
                        "利用者のファイルを消した:\n" + text)

    def test_an_old_marker_with_a_holder_is_still_taken_over(self):
        """更新が作った目印（holder.txt 入り）は、これまでどおり回収すること。"""
        os.makedirs(self.lock)
        with io.open(os.path.join(self.lock, "holder.txt"), "wb") as f:
            f.write(b"NetBeltUpdate_9_99999\r\n")
        self._age(self.lock)

        code, text = self._run()

        self.assertEqual(code, 0, "古い目印で更新が止まった:\n" + text)
        self.assertIn("取り除きました", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A", text)
        self.assertFalse(os.path.exists(self.lock), "目印が残った:\n" + text)

    def test_an_old_empty_marker_is_still_taken_over(self):
        """md の直後で止まった目印（空）も、これまでどおり回収すること。"""
        os.makedirs(self.lock)
        self._age(self.lock)

        code, text = self._run()

        self.assertEqual(code, 0, "古い目印で更新が止まった:\n" + text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A", text)
        self.assertFalse(os.path.exists(self.lock), "目印が残った:\n" + text)


if __name__ == "__main__":
    unittest.main()
