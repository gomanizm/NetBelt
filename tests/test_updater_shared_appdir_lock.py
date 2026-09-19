"""同じインストール先への更新が重なったら、後から来たほうが中止することを検証する。

一時名の exe は実行ごとに変わるようになった（tests/test_updater_shared_appdir.py）
が、インストール先そのものは相変わらず共有されていた。

実測（cx5b-release の r02_shared_appdir.py、62357d1）: updater.bat の写しに
「move の直後で待つ」関門を入れて A を止め、B を最後まで走らせてから A を
再開すると、

  * A は A の版を承認したのに、[6/6] で起動したのは B の exe だった。
    それでも A・B の両方が「更新が完了しました！」を出した。
  * 関門なしで自然に重ねた試験では、A の exe と B の同梱ファイルが混在し、
    B は「NetBelt.exe は旧版のままです」と事実と違う案内を出した
    （実際には A の新しい exe が据わっていた）。

直し方（利用者の決定 2026-09-20 / release-02）: インストール先を変える
最初の処理より前に、インストール先へ md で目印フォルダを作って排他を取る。
取れなかった更新は『別の更新が進行中です』と伝え、インストール先を何も
変えずに中止する（ダウンロード済みのファイルの扱いは既存の中止と同じ）。
異常終了で残った目印は、10 分より古ければ取り除いて続ける。終わるときは
成功・失敗・中止のどれでも目印を消す。

ここでは r02_shared_appdir.py と同じ関門を updater.bat の写しへ入れ、
A を move の直後で止めたまま B を走らせる。
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

# move /y の直後、差し替えの確認へ進む手前で待たせる。目印を持ったまま
# 止まる位置なので、ここで B を走らせると排他が効いているかが分かる。
ANCHOR = b'if not exist "!APP_DIR!NetBelt.exe" ('
GATE = [
    b'if not defined NB_TEST_GATE goto :nb_test_skip',
    b'echo reached>"!NB_TEST_GATE!.reached"',
    b':nb_test_wait',
    b'if not exist "!NB_TEST_GATE!.go" ( ping -n 2 127.0.0.1 >nul '
    b'& goto :nb_test_wait )',
    b':nb_test_skip',
]


def _gated_updater():
    """move の直後で待つ関門を 1 か所だけ入れた updater.bat を返す。"""
    lines = io.open(UPDATER, "rb").read().split(b"\r\n")
    idx = lines.index(ANCHOR)
    assert lines[idx - 1] == b":swapped", "関門の位置がずれている"
    return b"\r\n".join(lines[:idx] + GATE + lines[idx:])


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterSharedAppDirLockTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_appdirlock_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        with io.open(os.path.join(self.app_dir, "updater.bat"), "wb") as f:
            f.write(_gated_updater())
        self._write("NetBelt.exe", b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.gate = os.path.join(self.base, "gateA")

    def _write(self, name, body):
        with io.open(os.path.join(self.app_dir, name), "wb") as f:
            f.write(body)

    def _read(self, name):
        with io.open(os.path.join(self.app_dir, name), "rb") as f:
            return f.read()

    def _make_zip(self, tag):
        path = os.path.join(self.base, "%s.zip" % tag)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", "EXE_FROM_%s" % tag)
            z.writestr("zz_bundled.txt", "BUNDLED_FROM_%s" % tag)
        return path

    def _start(self, tag, gate=None):
        out_path = os.path.join(self.base, "%s.txt" % tag)
        out = io.open(out_path, "wb")
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        if gate:
            env["NB_TEST_GATE"] = gate
        else:
            env.pop("NB_TEST_GATE", None)
        proc = subprocess.Popen(
            '"%s" "%s" "%s"' % (os.path.join(self.app_dir, "updater.bat"),
                                self._make_zip(tag),
                                os.path.join(self.app_dir, "NetBelt.exe")),
            stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env)
        return proc, out, out_path

    def _snapshot(self):
        """インストール先の中身（相対パス→バイト列）を写し取る。"""
        taken = {}
        for root, _dirs, names in os.walk(self.app_dir):
            for name in names:
                path = os.path.join(root, name)
                with io.open(path, "rb") as f:
                    taken[os.path.relpath(path, self.app_dir)] = f.read()
        return taken

    def _output(self, out_path):
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def _wait_for_the_gate(self, proc, out_path):
        deadline = time.time() + 300
        while time.time() < deadline:
            if os.path.exists(self.gate + ".reached"):
                return
            if proc.poll() is not None:
                self.fail("A が関門へ着く前に終わった:\n" + self._output(out_path))
            time.sleep(0.02)
        self.fail("A が関門へ着かない:\n" + self._output(out_path))

    def test_the_second_update_aborts_and_the_first_one_keeps_its_version(self):
        """A が持っている間に来た B は、何も変えずに中止すること。"""
        a_proc, a_out, a_path = self._start("A", gate=self.gate)
        try:
            self._wait_for_the_gate(a_proc, a_path)
            self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A",
                             "前提が崩れている（A がまだ据えていない）")
            before = self._snapshot()

            b_proc, b_out, b_path = self._start("B")
            b_code = b_proc.wait(timeout=300)
            b_out.close()
            b_text = self._output(b_path)

            self.assertNotEqual(b_code, 0, "B が成功として返った:\n" + b_text)
            self.assertIn("別の更新が進行中です", b_text, b_text)
            # 重なったときに B が出していた、事実と違う案内
            self.assertNotIn("旧版のまま", b_text, b_text)
            self.assertNotIn("更新が完了しました", b_text, b_text)
            self.assertEqual(self._snapshot(), before,
                             "B がインストール先を書き換えた:\n" + b_text)
        finally:
            with io.open(self.gate + ".go", "wb") as f:
                f.write(b"go")
            a_code = a_proc.wait(timeout=300)
            a_out.close()
        a_text = self._output(a_path)

        self.assertEqual(a_code, 0, "A が失敗として返った:\n" + a_text)
        self.assertIn("更新が完了しました", a_text, a_text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A",
                         "A が承認した版とは別の exe が据わった:\n" + a_text)
        self.assertEqual(self._read("zz_bundled.txt"), b"BUNDLED_FROM_A",
                         "同梱ファイルが混ざった:\n" + a_text)

    def test_the_marker_is_gone_once_the_update_has_finished(self):
        """更新が終わったら、目印を残さないこと。"""
        proc, out, out_path = self._start("A")
        code = proc.wait(timeout=300)
        out.close()
        text = self._output(out_path)

        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, LOCK_NAME)),
                         "目印が残った:\n" + text)

    def test_a_marker_left_by_a_crash_is_taken_over_once_it_is_old(self):
        """異常終了で残った古い目印は、取り除いて続けること。"""
        stale = os.path.join(self.app_dir, LOCK_NAME)
        os.makedirs(stale)
        old = time.time() - 40 * 60
        os.utime(stale, (old, old))

        proc, out, out_path = self._start("A")
        code = proc.wait(timeout=300)
        out.close()
        text = self._output(out_path)

        self.assertEqual(code, 0, "古い目印で更新が止まった:\n" + text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A", text)
        self.assertFalse(os.path.exists(stale), "目印が残った:\n" + text)

    def test_a_marker_that_is_still_fresh_stops_the_update(self):
        """まだ新しい目印は、動いている更新のものとして扱うこと。"""
        fresh = os.path.join(self.app_dir, LOCK_NAME)
        os.makedirs(fresh)

        proc, out, out_path = self._start("A")
        code = proc.wait(timeout=300)
        out.close()
        text = self._output(out_path)

        self.assertNotEqual(code, 0, "新しい目印を無視して進んだ:\n" + text)
        self.assertIn("別の更新が進行中です", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"OLD-EXE", text)
        self.assertTrue(os.path.exists(fresh), "他の更新の目印を消した:\n" + text)


if __name__ == "__main__":
    unittest.main()
