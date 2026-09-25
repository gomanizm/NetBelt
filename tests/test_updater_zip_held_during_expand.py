"""updater.bat が、照合した ZIP の実体を展開し終えるまで手放さないことの回帰。

updater.bat は ZIP の SHA-256 を powershell で確かめ、その powershell が
終わってから、別の powershell が同じ「パス」を Expand-Archive で開き直して
いた。確かめたのはバイト列、開き直すのは名前なので、その間に同じ名前の中身を
別の有効な ZIP へ差し替えれば、照合していないものが据わる。

実測（検査役 cx7a-verify-release の p01_zip_swap_after_hash.py、f4cad23）:
:zip_sha_ok の直後（288 行のハッシュ照合が通った後・303 行の Expand-Archive の
手前）に関門を入れ、そこで ZIP を別の有効な ZIP へ置き換えて再開すると

    rc = 0、『更新が完了しました！』
    据わった exe = EXE_FROM_EVIL / 同梱ファイル = BUNDLED_FROM_EVIL

窓の広さ（p01b_window_size.py、関門なし）は平均 1571 ms（最小 841 / 最大
1986、5 回）。powershell を 2 回に分けていることによる起動 1 回ぶんで、その印を
見張って差し替える相手を置くと 5/5 で EXE_FROM_EVIL が据わった。
tests/test_updater_zip_hash_argument.py が見ていたのは updater.bat を起動する
「前」の差し替えだけで、照合と展開の間は通っていなかった。

直した形: 288 行と 303 行の powershell を 1 本にまとめ、ZIP を書き込み拒否の
共有モード（[IO.FileShare]::Read）で開いたまま Get-FileHash -InputStream で
照合し、同じハンドルを握ったまま Expand-Archive まで済ませる。実測
（e1_hold_share.py）: 掴んでいる間、copyfile / os.replace / rename / 書き込み
open はすべて PermissionError で拒まれ、Expand-Archive は成功した。

前提: 更新フォルダ（%TEMP%\\NetBeltUpdates）へ書ける相手＝同じ利用者の権限で
既にコードを実行できている相手なので、これは権限昇格の話ではない。
stage_for_apply／ZIP_SHA で入れた防御そのものの抜け穴を塞ぐもの。
"""
import hashlib
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

# 展開を始める場所。ここの直前で止めれば「照合が済んで、まだ展開して
# いない」瞬間になる。powershell を分けていた頃はここが別プロセスの
# 先頭だったので、この目印は直す前後のどちらでも同じ意味を持つ。
ANCHOR = (b"Expand-Archive -LiteralPath $env:PS_ZIP "
          b"-DestinationPath $env:PS_DEST -Force")

# 関門。NB_GATE_EXPAND が指す名前で「着いた」を知らせ、「進め」が
# 置かれるまで待つ。'!' は遅延展開に食われるので使わない。
GATE = (b"if ($env:NB_GATE_EXPAND) { "
        b"Set-Content -LiteralPath ($env:NB_GATE_EXPAND + '.reached') "
        b"-Value 'x'; "
        b"while (-not (Test-Path -LiteralPath "
        b"($env:NB_GATE_EXPAND + '.go'))) { Start-Sleep -Milliseconds 20 } "
        b"}; ")


def _gated_updater():
    """展開の直前で止まる updater.bat を返す。"""
    data = io.open(UPDATER, "rb").read()
    assert data.count(ANCHOR) == 1, "展開の呼び出しが 1 つではない"
    return data.replace(ANCHOR, GATE + ANCHOR)


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterHoldsZipUntilExpandedTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_ziphold_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        with io.open(self.updater, "wb") as f:
            f.write(_gated_updater())
        with io.open(os.path.join(self.app_dir, "NetBelt.exe"), "wb") as f:
            f.write(b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.gate = os.path.join(self.base, "gate")

    def _read(self, name):
        with io.open(os.path.join(self.app_dir, name), "rb") as f:
            return f.read()

    def _make_zip(self, tag, name=None):
        path = os.path.join(self.base, name or ("NetBelt-apply-%s.zip" % tag))
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", "EXE_FROM_%s" % tag)
            z.writestr("zz_bundled.txt", "BUNDLED_FROM_%s" % tag)
        return path

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with io.open(path, "rb") as f:
            digest.update(f.read())
        return digest.hexdigest()

    def _swap_attempts(self, target, evil_a, evil_b):
        """照合が済んだ ZIP を差し替える試み。拒まれた理由を名前ごとに返す。"""
        results = {}
        for name, call in (
                ("copyfile", lambda: shutil.copyfile(evil_a, target)),
                ("os.replace", lambda: os.replace(evil_b, target))):
            try:
                call()
                results[name] = None
            except OSError as e:
                results[name] = e.__class__.__name__
        return results

    def test_the_zip_cannot_be_swapped_between_the_check_and_the_expand(self):
        """照合が通った後・展開の前に、同じ名前の中身を差し替えられないこと。"""
        good = self._make_zip("GOOD")
        expected = self._sha256(good)
        evil_a = self._make_zip("EVIL", "evil_a.zip")
        evil_b = self._make_zip("EVIL", "evil_b.zip")

        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        env["NB_GATE_EXPAND"] = self.gate
        out_path = os.path.join(self.base, "out.txt")
        out = io.open(out_path, "wb")
        self.addCleanup(out.close)
        proc = subprocess.Popen(
            '"%s" "%s" "%s" "%s"' % (
                self.updater, good,
                os.path.join(self.app_dir, "NetBelt.exe"), expected),
            stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env)

        def release():
            with io.open(self.gate + ".go", "wb") as f:
                f.write(b"go")
        self.addCleanup(release)

        deadline = time.time() + 300
        while time.time() < deadline:
            if os.path.exists(self.gate + ".reached"):
                break
            self.assertIsNone(proc.poll(), "関門の前に終わった")
            time.sleep(0.02)
        else:
            self.fail("関門へ着かなかった")

        swaps = self._swap_attempts(good, evil_a, evil_b)
        release()
        code = proc.wait(timeout=300)
        out.close()
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            text = f.read()

        got_through = [k for k, v in swaps.items() if v is None]
        self.assertEqual(
            got_through, [],
            "照合が通った後なのに ZIP を差し替えられた: %s\n%s"
            % (swaps, text))
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_GOOD",
                         "照合していない中身が据わった:\n" + text)
        self.assertEqual(self._read("zz_bundled.txt"), b"BUNDLED_FROM_GOOD",
                         "同梱ファイルが入れ替わった:\n" + text)
        self.assertEqual(code, 0, text)
        self.assertIn("更新が完了しました", text, text)


if __name__ == "__main__":
    unittest.main()
