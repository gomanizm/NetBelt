"""TEMP のパスに角括弧があっても、更新が当たることの回帰（release-01）。

updater.bat は ZIP を %TEMP% の下の作業フォルダ（NetBeltUpdate_N_M\\zip）へ
展開する。展開は powershell の Expand-Archive に任せていて、ZIP 側は
-LiteralPath で渡していたが、展開先は -DestinationPath へそのまま渡していた。
Windows PowerShell 5.1 の Expand-Archive（Microsoft.PowerShell.Archive
1.0.1.0）は、展開先があるかを中で Test-Path -Path で確かめる。-Path は
ワイルドカードとして読むので、[lab] は「l・a・b のどれか 1 文字」になり、
bat が先に mkdir で作った展開先を「無い」と判定して、同じフォルダを
New-Item -ErrorAction Stop で作ろうとして止まる。

実測（470c538、TEMP=...\\Temp[lab]、ZIP も実アプリと同じく TEMP の下）:

    [4/6] ZIPファイルを展開中...
    エラー: An item with the specified name ...\\Temp[lab]\\NetBeltUpdate_1_22308\\zip already exists.
    エラー: ZIPファイルの展開に失敗しました
    rc = 1、NetBelt.exe は旧版のまま

閉じない [ だけ（...\\Temp[x）なら "The specified wildcard character pattern
is not valid"、] だけでも "already exists" で止まる。どれもインストール先は
変えずに止まるが、TEMP を変えない限り自動更新は毎回失敗する。

直した形: Expand-Archive へ渡す直前に、展開先の [ ] ` の前へ ` を付け、
ワイルドカードとして読まれない形にする。
[WildcardPattern]::Escape は Windows PowerShell 5.1 では ` をエスケープ
しないので使わない（実測: TEMP が ...\\a`] のとき、展開は「成功」したのに
中身は a``] という別のフォルダへ出た）。
.NET の ZipArchive で展開する形も比べた（掴んでいるハンドルから読めるので
試した名前はすべて通った）が、差し替え防止の回帰テスト
（test_updater_zip_held_during_expand.py）は Expand-Archive の呼び出しの
直前に関門を差し込む作りなので、Expand-Archive を残すほうを選んだ。
照合から展開の終わりまで ZIP を掴んだままにする形は変えていない
（下の関門つきのテストで、角括弧のある TEMP でも差し替えが拒まれることを見る）。
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

# 展開を始める場所（test_updater_zip_held_during_expand.py と同じ目印）。
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

BACKTICK = chr(96)


def _runnable_exe():
    """据える新しい exe。起動できる本物にして、[6/6] まで素直に通す。"""
    path = os.path.join(os.environ["SystemRoot"], "System32", "rundll32.exe")
    with io.open(path, "rb") as f:
        return f.read()


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterTempBracketsTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_upd_brackets_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(b"OLD-EXE")
        with io.open(os.path.join(self.app_dir, "README.txt"), "wb") as f:
            f.write(b"OLD-README")
        self.new_exe = _runnable_exe()

    def _read(self, *parts):
        path = os.path.join(self.app_dir, *parts)
        if not os.path.exists(path):
            return None
        with io.open(path, "rb") as f:
            return f.read()

    def _make_zip(self, path, exe, readme):
        # 配布 ZIP と同じく、サブフォルダ（mibs/）のあるものにする。
        # Expand-Archive はサブフォルダを作るところでも展開先のパスを使う。
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("NetBelt.exe", exe)
            z.writestr("README.txt", readme)
            z.writestr("mibs/README.md", "NEW-MIBS")

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with io.open(path, "rb") as f:
            digest.update(f.read())
        return digest.hexdigest()

    def _prepare(self, temp_name, updater_bytes=None):
        """角括弧入りの TEMP と、その下の更新フォルダに置いた ZIP を用意する。"""
        updater = os.path.join(self.app_dir, "updater.bat")
        if updater_bytes is None:
            with io.open(UPDATER, "rb") as f:
                updater_bytes = f.read()
        with io.open(updater, "wb") as f:
            f.write(updater_bytes)
        temp = os.path.join(self.base, temp_name)
        # 実アプリと同じく、ZIP は %TEMP%\NetBeltUpdates の適用用の写し
        updates = os.path.join(temp, "NetBeltUpdates")
        os.makedirs(updates)
        zip_path = os.path.join(updates, "NetBelt-apply-test.zip")
        self._make_zip(zip_path, self.new_exe, "NEW-README")
        return updater, temp, zip_path, self._sha256(zip_path)

    def _start(self, updater, temp, zip_path, sha, extra_env=None):
        env = dict(os.environ)
        env["TEMP"] = temp
        env["TMP"] = temp
        env.update(extra_env or {})
        out_path = os.path.join(self.base, "out.txt")
        out = io.open(out_path, "wb")
        self.addCleanup(out.close)
        # 本番と同じ渡し方（引数ごとに引用符で包んだ 1 本の文字列）
        proc = subprocess.Popen(
            '"%s" "%s" "%s" "%s"' % (updater, zip_path, self.app_path, sha),
            stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env)
        return proc, out, out_path

    @staticmethod
    def _finish(proc, out, out_path):
        code = proc.wait(timeout=300)
        out.close()
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def _assert_updated(self, temp_name, code, text):
        self.assertEqual(self._read("NetBelt.exe"), self.new_exe,
                         "TEMP=%r で NetBelt.exe が差し替わらない:\n%s"
                         % (temp_name, text))
        self.assertEqual(self._read("README.txt"), b"NEW-README", text)
        self.assertEqual(self._read("mibs", "README.md"), b"NEW-MIBS", text)
        self.assertEqual(code, 0, text)
        self.assertNotIn("展開に失敗", text, text)
        self.assertIn("更新が完了しました", text, text)

    def _update_with_temp(self, temp_name):
        updater, temp, zip_path, sha = self._prepare(temp_name)
        code, text = self._finish(*self._start(updater, temp, zip_path, sha))
        self._assert_updated(temp_name, code, text)

    def test_brackets_in_temp_do_not_stop_the_update(self):
        """TEMP=...\\Temp[lab] でも更新が当たること（指摘の条件そのもの）。"""
        self._update_with_temp("Temp[lab]")

    def test_an_unclosed_bracket_in_temp_does_not_stop_the_update(self):
        """閉じない [ は、ワイルドカードとして不正なパターンになる。"""
        self._update_with_temp("Temp[x")

    def test_a_backtick_before_a_bracket_in_temp_does_not_stop_the_update(self):
        """` はワイルドカードのエスケープ文字。] の直前にあっても当たること。

        [WildcardPattern]::Escape で直すと、この名前では展開が a``] という
        別のフォルダへ出てしまう（実測）。
        """
        self._update_with_temp("Temp" + BACKTICK + "]")

    def test_the_zip_is_still_held_until_expanded(self):
        """角括弧のある TEMP でも、照合の後・展開の前に ZIP を差し替えられないこと。"""
        with io.open(UPDATER, "rb") as f:
            data = f.read()
        self.assertEqual(data.count(ANCHOR), 1, "展開の呼び出しが 1 つではない")
        updater, temp, zip_path, sha = self._prepare(
            "Temp[lab]", data.replace(ANCHOR, GATE + ANCHOR))
        evil_a = os.path.join(self.base, "evil_a.zip")
        evil_b = os.path.join(self.base, "evil_b.zip")
        self._make_zip(evil_a, "EXE_FROM_EVIL", "BUNDLED_FROM_EVIL")
        self._make_zip(evil_b, "EXE_FROM_EVIL", "BUNDLED_FROM_EVIL")
        gate = os.path.join(self.base, "gate")

        proc, out, out_path = self._start(updater, temp, zip_path, sha,
                                          {"NB_GATE_EXPAND": gate})

        def release():
            with io.open(gate + ".go", "wb") as f:
                f.write(b"go")
        self.addCleanup(release)

        deadline = time.time() + 300
        while time.time() < deadline:
            if os.path.exists(gate + ".reached"):
                break
            self.assertIsNone(proc.poll(), "関門の前に終わった")
            time.sleep(0.02)
        else:
            self.fail("関門へ着かなかった")

        swaps = {}
        for name, call in (
                ("copyfile", lambda: shutil.copyfile(evil_a, zip_path)),
                ("os.replace", lambda: os.replace(evil_b, zip_path))):
            try:
                call()
                swaps[name] = None
            except OSError as e:
                swaps[name] = e.__class__.__name__
        release()
        code, text = self._finish(proc, out, out_path)

        self.assertEqual([k for k, v in swaps.items() if v is None], [],
                         "照合が通った後なのに ZIP を差し替えられた: %s\n%s"
                         % (swaps, text))
        self._assert_updated("Temp[lab]", code, text)


if __name__ == "__main__":
    unittest.main()
