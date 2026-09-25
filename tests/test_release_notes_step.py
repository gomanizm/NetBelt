"""リリースの「Build release notes」が、本文の無い節を弾くことの検証。

.github/workflows/build-release.yml のこのステップは、CHANGELOG.md から
対象版の節を切り出し、空なら止める。ところが空かどうかを見るのは切り出した
「後」で、範囲を作る前に start と end の大小を見ていなかった。PowerShell の
`$a[3..2]` は空配列ではなく逆順の 2 要素を返すので、本文が 1 行も無いときに
空チェックが素通りする。

実測（検査役 cx7a-verify-release の p04_release_notes.ps1、切り出し部分を
そのまま関数にしてメモリ上の CHANGELOG へ当てた。PSVersion 5.1.26100.9444）:

    case A 見出しが隣接（## [1.3.1] の次の行が ## [1.3.0]）
      start=3 end=3 -> 通過した
      section = '## [1.3.0] - 2026-09-01\\n## [1.3.1] - 2026-09-23'
      ＝ 次版の見出しと対象版の見出しが、逆順のまま本文として採用された
    case C 対象の見出しがファイル末尾で本文なし（次の見出しも無い）
      start=3 end=3（end は $lines.Count）-> 通過した
      section = '## [1.3.1] - 2026-09-23'
    case B 見出しの後に空行が 1 つだけ -> 例外「[1.3.1] の節が空です」（意図どおり）
    case D 本文あり -> section = '- fixed something'（正常）

現状の CHANGELOG.md には ## [1.3.1] の節がまだ無いので今は踏んでいないが、
見出しだけを足してタグを打つと踏む。壊れるのは公開されるリリースノートの
見た目だけで、配布物そのものには影響しない。

直した形: 範囲を作る前に弾く。`$null -eq $start` の判定の直後へ
`if ($start -ge $end) { throw ... }` を足す。$end は次の見出しの位置か
$lines.Count なので、$start -ge $end は「本文が 1 行も無い」と同値。
空白だけの節のために、既存の `-not $section` はそのまま残す。

ここでは run ブロックを yml から抜き出し、隔離した作業場所の PowerShell に
実行させる（手元に pwsh が無いので Windows PowerShell 5.1。逆順の範囲
演算子はどちらも同じ言語仕様）。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "build-release.yml")
STEP_NAME = "Build release notes"
VERSION = "1.3.1"
# ワークフローが差し込む GitHub の式。テストでは版を直に埋める。
VERSION_EXPR = "${{ steps.version.outputs.version }}"


def _notes_step():
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


@unittest.skipUnless(os.name == "nt", "PowerShell が要る")
class ReleaseNotesStepTest(unittest.TestCase):

    def setUp(self):
        self.script, shell = _notes_step()
        self.assertIsNotNone(self.script, "run ブロックが見つからない")
        self.assertEqual(shell, "pwsh",
                         "前提が崩れている（このテストは PowerShell を前提にしている）")
        self.assertIn(VERSION_EXPR, self.script, "版の埋め込み方が変わっている")
        self.script = self.script.replace(VERSION_EXPR, VERSION)
        self.ws = tempfile.mkdtemp(prefix="netbelt_notesstep_")
        self.addCleanup(shutil.rmtree, self.ws, True)
        os.makedirs(os.path.join(self.ws, ".github"))
        shutil.copyfile(
            os.path.join(REPO_ROOT, ".github", "release-body.md"),
            os.path.join(self.ws, ".github", "release-body.md"))

    def _changelog(self, *lines):
        with io.open(os.path.join(self.ws, "CHANGELOG.md"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    def _run_step(self):
        script = os.path.join(self.ws, "step.ps1")
        with io.open(script, "w", encoding="utf-8-sig", newline="") as f:
            f.write(self.script)
        env = dict(os.environ)
        env["GITHUB_REF_NAME"] = "v" + VERSION
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", "$ErrorActionPreference = 'stop'; . '%s'" % script],
            cwd=self.ws, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        return proc.returncode, proc.stdout.decode("mbcs", "replace")

    def _notes(self):
        path = os.path.join(self.ws, "release_notes.md")
        if not os.path.exists(path):
            return None
        with io.open(path, encoding="utf-8-sig") as f:
            return f.read()

    def test_a_section_with_a_body_is_published(self):
        """本文のある節は、これまでどおり通ること。"""
        self._changelog("# Changelog", "",
                        "## [1.3.1] - 2026-09-23",
                        "- fixed something", "",
                        "## [1.3.0] - 2026-09-01",
                        "- old")

        code, out = self._run_step()

        self.assertEqual(code, 0, out)
        notes = self._notes()
        self.assertIn("- fixed something", notes, out)
        self.assertNotIn("## [1.3.0]", notes,
                         "次の版の見出しまで本文に入った:\n" + notes)

    def test_a_heading_followed_by_the_next_heading_is_refused(self):
        """次版の見出しが隣接した（本文なしの）節は、公開しないこと。"""
        self._changelog("# Changelog", "",
                        "## [1.3.1] - 2026-09-23",
                        "## [1.3.0] - 2026-09-01",
                        "- something old")

        code, out = self._run_step()

        self.assertNotEqual(
            code, 0,
            "本文の無い節を通した（release_notes.md=%r）:\n%s"
            % (self._notes(), out))
        self.assertIsNone(self._notes(),
                          "止めたのにリリースノートを書いた:\n" + out)

    def test_a_heading_at_the_end_of_the_file_is_refused(self):
        """対象の見出しが末尾で本文が無い節も、公開しないこと。"""
        self._changelog("# Changelog", "",
                        "## [1.3.1] - 2026-09-23")

        code, out = self._run_step()

        self.assertNotEqual(
            code, 0,
            "本文の無い節を通した（release_notes.md=%r）:\n%s"
            % (self._notes(), out))
        self.assertIsNone(self._notes(),
                          "止めたのにリリースノートを書いた:\n" + out)

    def test_a_section_of_blank_lines_is_still_refused(self):
        """空行だけの節も、これまでどおり弾くこと。"""
        self._changelog("# Changelog", "",
                        "## [1.3.1] - 2026-09-23",
                        "", "   ", "",
                        "## [1.3.0] - 2026-09-01",
                        "- old")

        code, out = self._run_step()

        self.assertNotEqual(code, 0, out)

    def test_a_missing_section_is_still_refused(self):
        """節そのものが無いときも、これまでどおり弾くこと。"""
        self._changelog("# Changelog", "",
                        "## [1.3.0] - 2026-09-01",
                        "- old")

        code, out = self._run_step()

        self.assertNotEqual(code, 0, out)

    def test_the_current_changelog_still_builds_its_notes(self):
        """いま手元にある CHANGELOG.md の先頭の節が、通ること。"""
        shutil.copyfile(os.path.join(REPO_ROOT, "CHANGELOG.md"),
                        os.path.join(self.ws, "CHANGELOG.md"))
        with io.open(os.path.join(REPO_ROOT, "CHANGELOG.md"),
                     encoding="utf-8") as f:
            headings = [l for l in f.read().splitlines()
                        if l.startswith("## [")]
        self.assertTrue(headings, "CHANGELOG.md に版の見出しが無い")
        version = headings[0].split("[", 1)[1].split("]", 1)[0]
        self.script = self.script.replace("'%s'" % VERSION, "'%s'" % version)

        code, out = self._run_step()

        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
