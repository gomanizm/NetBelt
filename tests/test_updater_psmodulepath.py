"""PowerShell 7 の中から起動されても、updater.bat が展開まで進めることの回帰。

1.3.1 のタグで走ったリリースのワークフロー（GitHub の windows-latest、
run 36097480601）で、updater.bat を実物で走らせるテスト 8 件
（test_updater_zip_hash_argument / zip_held_during_expand /
old_script_new_payload の 2 件 / temp_brackets の 4 件）が、角括弧の無い
普通の TEMP でも同じ形で落ちた:

    [4/6] ZIPファイルを展開中...
    エラー: The term 'Get-FileHash' is not recognized as the name of a cmdlet, ...
    エラー: ZIPファイルの展開に失敗しました

ランナーはテストの段を PowerShell 7（pwsh）の中で走らせる。pwsh は環境変数
PSModulePath の先頭に自分のモジュールの置き場所（...\\PowerShell\\7\\Modules
など）を足し、そこから起動された python → cmd → updater.bat の powershell
（Windows PowerShell 5.1）はそれを引き継ぐ。5.1 は Get-FileHash を探して、
先頭にある 7 用の Microsoft.PowerShell.Utility（ModuleVersion 7.0.0.0・
CompatiblePSEditions Core・入れ子は dll の名前だけ）を読み込む。dll の名前は
5.1 自身の同名の dll に解決されるので Write-Host などはそのまま動くが、
5.1 では Get-FileHash は dll ではなく同梱の psm1 の関数なので、7 用の
マニフェスト経由では現れず「認識されません」になる。Expand-Archive
（Microsoft.PowerShell.Archive）も同じく先頭の 7 用を選ぶ。利用者でも、
PowerShell 7 のターミナル（Windows Terminal の既定など）から NetBelt を
起動していれば、1.3.1 から次の版への更新が毎回この形で失敗する。

実測（このパソコンに PowerShell 7 は無いので、7 用の同梱モジュールと同じ形の
マニフェストだけを並べた置き場所を PSModulePath の先頭に差し込んだ。下の
_plant_foreign_modules がそれ）: 5.1 は差し込んだ Microsoft.PowerShell.Utility
7.0.0.0 を選び、powershell を直に叩くと Get-FileHash は「用語
'Get-FileHash' は、コマンドレット、関数、スクリプト ファイル、または操作
可能なプログラムの名前として認識されません。」（上のログの日本語版）、
Write-Host と Get-Item はそのまま動いた。Expand-Archive は差し込んだ
Archive の関数が呼ばれた。この環境で 5bdb573 の updater.bat を走らせると、
ワークフローと一字一句同じ
「エラー: The term 'Get-FileHash' is not recognized as the name of a cmdlet,
...」「エラー: ZIPファイルの展開に失敗しました」で exit 1、NetBelt.exe は
旧版のまま。7.x でしか読めない
指定（PowerShellVersion='7.0'）のマニフェストでも「The 'Write-Host' command
was found in the module 'Microsoft.PowerShell.Utility', but the module could
not be loaded.」「展開に失敗しました」で同じく止まった。

ワークフローで落ちたのは、ハッシュを渡して updater.bat を走らせる 8 件だけで、
渡さない試験（自分を上書きする旧版の着地の 2 件など）は通っていた。7 に
同梱の Archive が選ばれていたとしても、普通の TEMP なら展開はできていたと
見られる（推測。ランナーでは確かめていない）。ここで差し込む Archive を
「呼ばれたら失敗する関数」にしているのは、先頭に足された Archive が
選ばれないこと（角括弧の逃がし方が前提にしている
5.1 の 1.0.1.0 が使われること）を見るため。同じ置き場所を PSModulePath に
足して、落ちた 8 件を含む 4 ファイルを 5bdb573 で流すと、同じ 8 件が
ワークフローと同じ形で落ちた（出力を載せる 6 件は上のログと同じ
Get-FileHash の行、関門つきの 2 件は「1 is not None : 関門の前に終わった」）。
ほかにハッシュを渡さない 2 件も、差し込んだ Archive が選ばれて落ちた。
直した後は 15 件すべて通る。

直した形: updater.bat が最初の powershell を起動する前に、PSModulePath を
Windows PowerShell 5.1 の標準の置き場所（%SystemRoot%\\system32\\
WindowsPowerShell\\v1.0\\Modules）だけにする。展開・照合・目印の判定の
powershell はどれもそこにある標準のモジュールしか使わないので、利用者が
足した置き場所は要らない（足されたものに Archive の別の版があると、角括弧の
逃がし方が前提にしている 1.0.1.0 とは違う Expand-Archive が選ばれる）。
呼び出し元の値は控えておき、NetBelt を起動し直す直前に戻す（起動し直した
NetBelt の環境は変えない）。
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

# 展開を始める場所（test_updater_zip_held_during_expand.py と同じ目印と関門）。
ANCHOR = (b"Expand-Archive -LiteralPath $env:PS_ZIP "
          b"-DestinationPath $env:PS_DEST -Force")
GATE = (b"if ($env:NB_GATE_EXPAND) { "
        b"Set-Content -LiteralPath ($env:NB_GATE_EXPAND + '.reached') "
        b"-Value 'x'; "
        b"while (-not (Test-Path -LiteralPath "
        b"($env:NB_GATE_EXPAND + '.go'))) { Start-Sleep -Milliseconds 20 } "
        b"}; ")

# NetBelt を起動し直す直前（errorlevel を均す行の手前）に、そのときの
# PSModulePath を書き出す差し込み。'!' は遅延展開の中で値を読むためのもの。
LEVEL = b"cmd /d /c exit 0\r\n"
DUMP = b'(echo !PSModulePath!)>"!NB_ENV_DUMP!"\r\n'

# 差し込んだ Archive の Expand-Archive が呼ばれたときの印。
FOREIGN_MARK = "FOREIGN-ARCHIVE-MODULE"


def _manifest(path, version, cmdlets, functions, nested, psver="3.0"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="ascii", newline="\r\n") as f:
        f.write("@{\n"
                "ModuleVersion='%s'\n"
                "CompatiblePSEditions=@('Core')\n"
                "PowerShellVersion='%s'\n"
                "CmdletsToExport=@(%s)\n"
                "FunctionsToExport=@(%s)\n"
                "NestedModules=@('%s')\n"
                "}\n" % (version, psver,
                         ",".join("'%s'" % c for c in cmdlets),
                         ",".join("'%s'" % c for c in functions),
                         nested))


def _plant_foreign_modules(root, psver="3.0"):
    """PowerShell 7 の同梱モジュールに見立てた置き場所を root に作る。

    マニフェストは 7 の同梱のものと同じ形（Core 専用、入れ子は dll の名前
    だけで実体は置かない）。psver に '7.0' を渡すと、5.1 では読み込めない
    マニフェストになる。Archive は、呼ばれたら印を付けて失敗する関数にする
    （差し込んだものが選ばれたことを出力で見分けるため）。
    """
    _manifest(os.path.join(root, "Microsoft.PowerShell.Utility",
                           "Microsoft.PowerShell.Utility.psd1"),
              "7.0.0.0",
              ["Get-FileHash", "Write-Host", "Get-Date", "Start-Sleep",
               "Out-String", "Select-Object"],
              [], "Microsoft.PowerShell.Commands.Utility.dll", psver)
    _manifest(os.path.join(root, "Microsoft.PowerShell.Management",
                           "Microsoft.PowerShell.Management.psd1"),
              "7.0.0.0",
              ["Get-Item", "Get-ChildItem", "Test-Path", "Join-Path",
               "Set-Content", "New-Item", "Resolve-Path", "Remove-Item"],
              [], "Microsoft.PowerShell.Commands.Management.dll", psver)
    archive = os.path.join(root, "Microsoft.PowerShell.Archive")
    _manifest(os.path.join(archive, "Microsoft.PowerShell.Archive.psd1"),
              "1.2.5", [], ["Expand-Archive", "Compress-Archive"],
              "Microsoft.PowerShell.Archive.psm1", psver)
    with io.open(os.path.join(archive, "Microsoft.PowerShell.Archive.psm1"),
                 "w", encoding="ascii", newline="\r\n") as f:
        for name in ("Expand-Archive", "Compress-Archive"):
            f.write("function %s { throw '%s' }\n" % (name, FOREIGN_MARK))


def _runnable_exe():
    """据える新しい exe。起動できる本物にして、[6/6] まで素直に通す。"""
    path = os.path.join(os.environ["SystemRoot"], "System32", "rundll32.exe")
    with io.open(path, "rb") as f:
        return f.read()


@unittest.skipUnless(sys.platform == "win32", "cmd.exe と powershell が要る")
class UpdaterUnderForeignModulePathTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_upd_psmodpath_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(b"OLD-EXE")
        with io.open(os.path.join(self.app_dir, "README.txt"), "wb") as f:
            f.write(b"OLD-README")
        self.new_exe = _runnable_exe()
        self.modules = os.path.join(self.base, "pwsh7-modules")

    def _env(self, temp, psver="3.0"):
        """PowerShell 7 の中から起動されたときと同じ PSModulePath の環境。"""
        _plant_foreign_modules(self.modules, psver)
        env = {k: v for k, v in os.environ.items()
               if k.upper() != "PSMODULEPATH"}
        env["PSModulePath"] = (self.modules + os.pathsep
                               + os.environ.get("PSModulePath", ""))
        env["TEMP"] = temp
        env["TMP"] = temp
        return env

    def _read(self, *parts):
        path = os.path.join(self.app_dir, *parts)
        if not os.path.exists(path):
            return None
        with io.open(path, "rb") as f:
            return f.read()

    @staticmethod
    def _make_zip(path, exe, readme):
        # 配布 ZIP と同じく、サブフォルダ（mibs/）のあるものにする
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

    def _prepare(self, temp_name="temp", updater_bytes=None):
        """updater.bat と、TEMP の更新フォルダに置いた適用用の写しを用意する。"""
        updater = os.path.join(self.app_dir, "updater.bat")
        if updater_bytes is None:
            with io.open(UPDATER, "rb") as f:
                updater_bytes = f.read()
        with io.open(updater, "wb") as f:
            f.write(updater_bytes)
        temp = os.path.join(self.base, temp_name)
        updates = os.path.join(temp, "NetBeltUpdates")
        os.makedirs(updates)
        zip_path = os.path.join(updates, "NetBelt-apply-test.zip")
        self._make_zip(zip_path, self.new_exe, "NEW-README")
        return updater, temp, zip_path, self._sha256(zip_path)

    def _start(self, updater, zip_path, sha, env):
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

    def _assert_updated(self, code, text):
        self.assertNotIn("Get-FileHash", text, text)
        self.assertNotIn(FOREIGN_MARK, text, text)
        self.assertNotIn("展開に失敗", text, text)
        self.assertEqual(self._read("NetBelt.exe"), self.new_exe,
                         "NetBelt.exe が差し替わらない:\n" + text)
        self.assertEqual(self._read("README.txt"), b"NEW-README", text)
        self.assertEqual(self._read("mibs", "README.md"), b"NEW-MIBS", text)
        self.assertEqual(code, 0, text)
        self.assertIn("更新が完了しました", text, text)

    def test_the_planted_path_breaks_windows_powershell_by_itself(self):
        """前提: 差し込んだ置き場所だけで、5.1 の Get-FileHash と
        Expand-Archive が使えなくなること（updater.bat を通さずに見る）。"""
        probe = os.path.join(self.base, "probe.zip")
        self._make_zip(probe, b"x", b"x")
        env = self._env(os.path.join(self.base, "temp"))
        env["NB_PROBE"] = probe
        script = (
            "try { $null = Get-FileHash -LiteralPath $env:NB_PROBE "
            "-ErrorAction Stop; 'HASH-OK' } catch { 'HASH-BROKEN' }; "
            "try { Expand-Archive -LiteralPath $env:NB_PROBE "
            "-DestinationPath ($env:NB_PROBE + '.d') -Force; 'EXPAND-OK' } "
            "catch { 'EXPAND:' + $_.Exception.Message }")
        done = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            env=env, capture_output=True, stdin=subprocess.DEVNULL,
            timeout=120)
        text = done.stdout.decode("mbcs", "replace")
        self.assertIn("HASH-BROKEN", text, text)
        self.assertIn("EXPAND:" + FOREIGN_MARK, text, text)

    def test_a_matching_hash_applies_the_update(self):
        """照合を通って exe が差し替わること（ワークフローで落ちた形そのもの）。"""
        updater, temp, zip_path, sha = self._prepare()

        code, text = self._finish(
            *self._start(updater, zip_path, sha, self._env(temp)))

        self._assert_updated(code, text)
        self.assertFalse(os.path.exists(zip_path),
                         "当て終えたのに適用用の写しが残った:\n" + text)

    def test_a_module_path_windows_powershell_cannot_load_is_not_used(self):
        """7.x でしか読めない指定のマニフェストが先頭にあっても当たること。"""
        updater, temp, zip_path, sha = self._prepare()

        code, text = self._finish(*self._start(
            updater, zip_path, sha, self._env(temp, psver="7.0")))

        self._assert_updated(code, text)

    def test_a_zip_swapped_after_the_check_is_still_refused(self):
        """ハッシュが食い違えば、展開せずにその旨を伝えて中止すること。"""
        updater, temp, zip_path, sha = self._prepare()
        evil = os.path.join(self.base, "evil.zip")
        self._make_zip(evil, b"EXE_FROM_EVIL", "BUNDLED_FROM_EVIL")
        os.replace(evil, zip_path)

        code, text = self._finish(
            *self._start(updater, zip_path, sha, self._env(temp)))

        self.assertNotEqual(code, 0, text)
        self.assertIn("確認した時点から変わっています", text, text)
        self.assertNotIn("更新が完了しました", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"OLD-EXE",
                         "インストール先の exe を書き換えた:\n" + text)
        self.assertFalse(os.path.exists(zip_path),
                         "中止したのに適用用の写しが残った:\n" + text)

    def test_brackets_in_temp_still_update(self):
        """TEMP=...\\Temp[lab] でも当たること（角括弧の逃がし方が前提に
        している 5.1 の Expand-Archive が使われること）。"""
        updater, temp, zip_path, sha = self._prepare("Temp[lab]")

        code, text = self._finish(
            *self._start(updater, zip_path, sha, self._env(temp)))

        self._assert_updated(code, text)

    def test_the_zip_is_still_held_until_expanded(self):
        """照合の後・展開の前に、ZIP を差し替えられないこと。"""
        with io.open(UPDATER, "rb") as f:
            data = f.read()
        self.assertEqual(data.count(ANCHOR), 1, "展開の呼び出しが 1 つではない")
        updater, temp, zip_path, sha = self._prepare(
            "temp", data.replace(ANCHOR, GATE + ANCHOR))
        evil_a = os.path.join(self.base, "evil_a.zip")
        evil_b = os.path.join(self.base, "evil_b.zip")
        self._make_zip(evil_a, "EXE_FROM_EVIL", "BUNDLED_FROM_EVIL")
        self._make_zip(evil_b, "EXE_FROM_EVIL", "BUNDLED_FROM_EVIL")
        gate = os.path.join(self.base, "gate")
        env = self._env(temp)
        env["NB_GATE_EXPAND"] = gate

        proc, out, out_path = self._start(updater, zip_path, sha, env)

        def release():
            with io.open(gate + ".go", "wb") as f:
                f.write(b"go")
        self.addCleanup(release)

        deadline = time.time() + 300
        while time.time() < deadline:
            if os.path.exists(gate + ".reached"):
                break
            if proc.poll() is not None:
                code, text = self._finish(proc, out, out_path)
                self.fail("関門の前に終わった (rc=%d):\n%s" % (code, text))
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
        self._assert_updated(code, text)

    def test_the_restarted_app_keeps_the_callers_module_path(self):
        """起動し直す NetBelt には、呼び出し元の PSModulePath をそのまま渡すこと。"""
        with io.open(UPDATER, "rb") as f:
            data = f.read()
        self.assertEqual(data.count(LEVEL), 1, "均す行が 1 つではない")
        updater, temp, zip_path, sha = self._prepare(
            "temp", data.replace(LEVEL, DUMP + LEVEL))
        dump = os.path.join(self.base, "psmodulepath.txt")
        env = self._env(temp)
        env["NB_ENV_DUMP"] = dump

        code, text = self._finish(
            *self._start(updater, zip_path, sha, env))

        self._assert_updated(code, text)
        with io.open(dump, encoding="utf-8", errors="replace") as f:
            seen = f.read().strip()
        self.assertEqual(seen, env["PSModulePath"], text)


if __name__ == "__main__":
    unittest.main()
