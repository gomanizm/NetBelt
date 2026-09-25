"""リリースの「Create portable package」が、コピーの失敗を失敗として止めることを検証する。

.github/workflows/build-release.yml のこのステップは shell: cmd で、
copy と xcopy を並べているだけだった。cmd はステップの終了コードとして
最後のコマンドの値しか返さないので、途中の copy が失敗しても、最後の
mibs の xcopy が成功すればステップは成功になる。

実測（検証役 release-04）: run ブロックを抜き出し、GitHub の shell: cmd と
同じ `cmd /D /E:ON /V:OFF /S /C "CALL "step.cmd""` で実行した。
dist\\NetBelt.exe が無いと「指定されたファイルが見つかりません。」と出るのに
exit=0 で、portable から NetBelt.exe だけが欠けた（LICENSE が無い場合も
exit=0 で LICENSE.txt が欠けた）。続く Compress-Archive と Get-FileHash も
exit=0 で、exe の無い ZIP とその正しい SHA-256 ができた。この ZIP は
利用者側のダウンロード検証を通り、アプリの終了後に updater.bat が
「更新ファイルに NetBelt.exe が含まれていません」で失敗する。起きれば
全利用者に壊れた更新が配られる。

直し方: 各 mkdir・copy・xcopy の直後で失敗を見てステップを止め、ZIP を
作る前に portable\\NetBelt.exe と portable\\updater.bat があることも
確かめる。

ここでは run ブロックを yml から抜き出し、隔離した作業場所で GitHub と
同じ形の cmd に実行させる。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "build-release.yml")
STEP_NAME = "Create portable package"

# 配布物の中身（ワークフローが portable へ置くもの）
EXPECTED = ["CHANGELOG.txt", "LICENSE.txt", "NetBelt.exe", "README.txt",
            "THIRD-PARTY-NOTICES.txt", "mibs", "updater.bat"]


def _package_step():
    """ステップの run ブロックと shell を返す。"""
    with io.open(WORKFLOW, encoding="utf-8") as f:
        lines = f.read().splitlines()
    head = "- name: %s" % STEP_NAME
    start = next(i for i, l in enumerate(lines) if l.strip() == head)
    step_indent = len(lines[start]) - len(lines[start].lstrip())
    key_indent = step_indent + 2
    run, shell, i = None, None, start + 1
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped and indent <= step_indent:
            break
        if indent == key_indent and stripped == "run: |":
            body = []
            i += 1
            while i < len(lines):
                inner = lines[i]
                if inner.strip() and (len(inner) - len(inner.lstrip())
                                      <= key_indent):
                    break
                body.append(inner[key_indent + 2:])
                i += 1
            run = "\r\n".join(body).rstrip() + "\r\n"
            continue
        if indent == key_indent and stripped.startswith("shell:"):
            shell = stripped.split(":", 1)[1].strip()
        i += 1
    return run, shell


@unittest.skipUnless(os.name == "nt", "cmd.exe が要る")
class ReleasePackageStepTest(unittest.TestCase):

    def setUp(self):
        self.script, shell = _package_step()
        self.assertIsNotNone(self.script, "run ブロックが見つからない")
        self.assertEqual(shell, "cmd",
                         "前提が崩れている（このテストは shell: cmd を前提にしている）")
        self.ws = tempfile.mkdtemp(prefix="netbelt_pkgstep_")
        self.addCleanup(shutil.rmtree, self.ws, True)
        for name in ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.txt",
                     "updater.bat", "CHANGELOG.md"):
            shutil.copyfile(os.path.join(REPO_ROOT, name),
                            os.path.join(self.ws, name))
        os.makedirs(os.path.join(self.ws, "mibs"))
        self._write(os.path.join("mibs", "EXAMPLE-MIB.my"), b"mib")
        os.makedirs(os.path.join(self.ws, "dist"))
        self._write(os.path.join("dist", "NetBelt.exe"), b"MZ-new")

    def _write(self, rel, body):
        with io.open(os.path.join(self.ws, rel), "wb") as f:
            f.write(body)

    def _run_step(self):
        """GitHub Actions の shell: cmd と同じ呼び方で実行する。"""
        script = os.path.join(self.ws, "step.cmd")
        with io.open(script, "w", encoding="utf-8", newline="") as f:
            f.write(self.script)
        comspec = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
        proc = subprocess.run(
            '"%s" /D /E:ON /V:OFF /S /C "CALL "%s""' % (comspec, script),
            cwd=self.ws, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = proc.stdout.decode("mbcs", "replace")
        portable = os.path.join(self.ws, "portable")
        items = sorted(os.listdir(portable)) if os.path.isdir(portable) else None
        return proc.returncode, out, items

    def test_the_complete_package_passes(self):
        """材料がそろっていれば、成功して配布物がそろうこと。"""
        code, out, items = self._run_step()

        self.assertEqual(code, 0, out)
        self.assertEqual(items, EXPECTED, out)

    def test_a_missing_exe_fails_the_step(self):
        """dist\\NetBelt.exe が無ければ、ステップが失敗すること。"""
        os.remove(os.path.join(self.ws, "dist", "NetBelt.exe"))

        code, out, items = self._run_step()

        self.assertNotEqual(
            code, 0,
            "exe の無い配布物を作ったのに成功した（portable=%s）:\n%s"
            % (items, out))

    def test_a_missing_license_fails_the_step(self):
        """途中のどの copy が失敗しても、ステップが失敗すること。"""
        os.remove(os.path.join(self.ws, "LICENSE"))

        code, out, items = self._run_step()

        self.assertNotEqual(
            code, 0,
            "LICENSE の無い配布物を作ったのに成功した（portable=%s）:\n%s"
            % (items, out))

    def test_a_missing_updater_fails_the_step(self):
        """updater.bat が無ければ、ステップが失敗すること。"""
        os.remove(os.path.join(self.ws, "updater.bat"))

        code, out, items = self._run_step()

        self.assertNotEqual(
            code, 0,
            "updater.bat の無い配布物を作ったのに成功した（portable=%s）:\n%s"
            % (items, out))


if __name__ == "__main__":
    unittest.main()
