"""リリース本文の「パッケージ内容」が、実際に ZIP へ入れるものと一致することの回帰（release-doc-b）。

.github/release-body.md の「📦 パッケージ内容」は ZIP の中身の一覧だが、
updater.bat が載っていなかった。ワークフロー（build-release.yml）の
「Create portable package」は copy updater.bat portable\\ を行い、無ければ
ステップを失敗させる。続く「Create ZIP archive」は portable\\* をそのまま
ZIP にするので、updater.bat は必ず ZIP に入る。

実測（cx8b-release の repro_b_package_list.py）: 公開済みの v1.1.0 / v1.1.1 /
v1.2.0 / v1.3.0 の ZIP の最上位をすべて列挙すると、どの版にも updater.bat が
あるのに、一覧は NetBelt.exe・README.txt・LICENSE.txt・THIRD-PARTY-NOTICES.txt・
CHANGELOG.txt・mibs/ の 6 項目だけだった。一覧に無いので不要なものと見て
消されると、次の自動更新は「updater.batが見つかりません」で止まる。

直した形: 一覧へ updater.bat の 1 行を足した（自動更新が使うので消さないよう
添えた）。ここでは「Create portable package」の run ブロックを yml から抜き出し、
隔離した作業場所で GitHub の shell: cmd と同じ呼び方で実行して、できた portable
の最上位（＝ ZIP の中身）と一覧を突き合わせる。どちらかだけを変えると落ちる。
"""
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BODY = os.path.join(REPO_ROOT, ".github", "release-body.md")
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "build-release.yml")
STEP_NAME = "Create portable package"
_ITEM = re.compile(r"^- `([^`]+)`")


def _listed_names():
    """「パッケージ内容」の箇条書きに載っている名前（末尾の / は外す）を返す。"""
    with io.open(BODY, encoding="utf-8") as f:
        lines = f.read().splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith("### ") and "パッケージ内容" in l)
    names = []
    for line in lines[start + 1:]:
        if line.startswith("- "):
            m = _ITEM.match(line)
            if not m:
                raise AssertionError("名前を読み取れない行: %r" % line)
            names.append(m.group(1).rstrip("/"))
        elif names or line.startswith("#"):
            break
    return names


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
class ReleaseBodyPackageListTest(unittest.TestCase):

    def setUp(self):
        self.script, shell = _package_step()
        self.assertIsNotNone(self.script, "run ブロックが見つからない")
        self.assertEqual(shell, "cmd",
                         "前提が崩れている（このテストは shell: cmd を前提にしている）")
        self.ws = tempfile.mkdtemp(prefix="netbelt_pkglist_")
        self.addCleanup(shutil.rmtree, self.ws, True)
        for name in ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.txt",
                     "updater.bat", "CHANGELOG.md"):
            shutil.copyfile(os.path.join(REPO_ROOT, name),
                            os.path.join(self.ws, name))
        os.makedirs(os.path.join(self.ws, "mibs"))
        with io.open(os.path.join(self.ws, "mibs", "README.md"), "wb") as f:
            f.write(b"mibs")
        os.makedirs(os.path.join(self.ws, "dist"))
        with io.open(os.path.join(self.ws, "dist", "NetBelt.exe"), "wb") as f:
            f.write(b"MZ-new")

    def _packaged_names(self):
        """GitHub Actions の shell: cmd と同じ呼び方で実行し、portable の最上位を返す。"""
        script = os.path.join(self.ws, "step.cmd")
        with io.open(script, "w", encoding="utf-8", newline="") as f:
            f.write(self.script)
        comspec = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
        proc = subprocess.run(
            '"%s" /D /E:ON /V:OFF /S /C "CALL "%s""' % (comspec, script),
            cwd=self.ws, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = proc.stdout.decode("mbcs", "replace")
        self.assertEqual(proc.returncode, 0, out)
        return sorted(os.listdir(os.path.join(self.ws, "portable")))

    def test_the_premise_the_package_contains_the_updater(self):
        """前提: 配布物（＝ ZIP の中身）には NetBelt.exe と updater.bat が入る。"""
        packaged = self._packaged_names()
        self.assertIn("NetBelt.exe", packaged)
        self.assertIn("updater.bat", packaged)

    def test_the_package_list_matches_what_goes_into_the_zip(self):
        """「パッケージ内容」の一覧と、ZIP に入れるものが一致すること。"""
        packaged = self._packaged_names()
        listed = _listed_names()
        self.assertIn("NetBelt.exe", listed, "一覧を読み取れていない")

        self.assertEqual(
            sorted(set(packaged) - set(listed)), [],
            "ZIP に入るのに一覧に無いものがある（一覧=%s）" % sorted(listed))
        self.assertEqual(
            sorted(set(listed) - set(packaged)), [],
            "一覧にあるのに ZIP に入らないものがある（ZIP=%s）" % packaged)
        self.assertEqual(len(listed), len(set(listed)),
                         "一覧に同じ名前が 2 度ある: %s" % listed)


if __name__ == "__main__":
    unittest.main()
