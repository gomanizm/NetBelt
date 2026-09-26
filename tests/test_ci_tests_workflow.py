"""PR と main への push でテストを走らせるワークフロー（.github/workflows/tests.yml）が、
リリースの門番（build-release.yml の Run tests）と同じ環境でテストを走らせることの検証。

何が起きていたか
  1.3.1 まで、テストが GitHub の Windows ランナーで走るのは、タグの push と手動実行
  （build-release.yml）のときだけだった。手元（日本語の Windows・cp932・Python 3.12・
  pwsh なし・一般ユーザー）とランナー（Python 3.11・pwsh の中・管理者・TEMP が 8.3
  短縮名）は違い、手元で全件が通った中身で v1.3.1 のタグを打つと、ランナーで初めて
  12 件が落ち（updater 系 8 件は PowerShell 7 から起動したときの製品の不具合）、
  タグを打ち直した。
どう直したか
  PR・main への push・手動で走る tests.yml を足した。中身はリリースの Run tests までと
  同じにし、ずれたらここで落とす。PyYAML は依存に無いので字面で読む。確かめること:
  - pull_request・push・workflow_dispatch で起動し、push は main だけ
  - リリースと同じランナー・Python・pytest・依存・Run tests の呼び方と環境変数
    （足してよいのは表示の引数 -r… だけ）
  - Run tests に shell・defaults・working-directory を書かない（リリースと同じく
    既定の pwsh の中で、リポジトリの直下から走らせる）
  - テストの手順へ GITHUB_TOKEN を渡さない（製品は環境変数の GITHUB_TOKEN を読み、
    api.github.com への要求へ載せる）
  - 時間の上限がある（既定の 360 分では、固まったときに 6 時間止まる）
"""
import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")
RELEASE = os.path.join(WORKFLOWS, "build-release.yml")
TESTS = os.path.join(WORKFLOWS, "tests.yml")
STEP_NAME = "Run tests"


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _on_block(text):
    """トップレベルの on: の直下の行"""
    lines, inside = [], False
    for line in text.splitlines():
        if re.match(r"^on\s*:", line):
            inside = True
            continue
        if inside:
            if line.strip() and not line[0].isspace() and not line.startswith("#"):
                break
            lines.append(line)
    return lines


def _events(text):
    """トップレベルの on: の直下にある event の名前"""
    names = []
    for line in _on_block(text):
        m = re.match(r"^  ([a-z_]+)\s*:", line)
        if m:
            names.append(m.group(1))
    return names


def _event_body(text, event):
    """on: の中の event の下の行（字下げの深い行）"""
    body, inside = [], False
    for line in _on_block(text):
        if re.match(r"^  %s\s*:" % event, line):
            inside = True
            continue
        if inside:
            if re.match(r"^  \S", line):
                break
            body.append(line)
    return "\n".join(body)


def _one(pattern, text):
    found = re.findall(pattern, text, re.MULTILINE)
    return found[0] if len(found) == 1 else found


def _step(text, name):
    """名前の一致するステップの行（先頭の '- name:' を含む）を返す"""
    lines = text.splitlines()
    head = "- name: %s" % name
    start = next(i for i, l in enumerate(lines) if l.strip() == head)
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return body


def _step_block(step, key):
    """ステップの中の key: の下の行（空行を除き、前後の空白を落とす）"""
    out, inside, key_indent = [], False, None
    for line in step[1:]:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped in ("%s:" % key, "%s: |" % key):
            inside, key_indent = True, indent
            continue
        if inside:
            if stripped and indent <= key_indent:
                break
            if stripped:
                out.append(stripped)
    return out


def _step_env(step):
    out = {}
    for line in _step_block(step, "env"):
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


class CiTestsWorkflowTest(unittest.TestCase):

    def setUp(self):
        self.assertTrue(os.path.exists(TESTS),
                        "PR と push でテストを走らせるワークフロー %s が無い"
                        "（テストがランナーで走るのはタグと手動実行のときだけ）"
                        % os.path.relpath(TESTS, REPO_ROOT))
        self.release = _read(RELEASE)
        self.tests = _read(TESTS)

    def test_it_runs_on_pull_requests_pushes_to_main_and_by_hand(self):
        events = _events(self.tests)
        for event in ("pull_request", "push", "workflow_dispatch"):
            self.assertIn(event, events)
        # PR のブランチへの push で同じ中身を 2 回走らせない
        self.assertRegex(_event_body(self.tests, "push"),
                         r"branches:\s*\[\s*main\s*\]")

    def test_it_uses_the_same_runner_python_and_pytest_as_the_release(self):
        for pattern in (r"^\s*runs-on:\s*(\S+)",
                        r"^\s*python-version:\s*(\S+)",
                        r"^\s*uses:\s*(actions/checkout@\S+)",
                        r"^\s*uses:\s*(actions/setup-python@\S+)",
                        r"pip install (pytest==\S+)",
                        r"pip install -r (\S+)"):
            with self.subTest(pattern=pattern):
                self.assertEqual(_one(pattern, self.tests),
                                 _one(pattern, self.release))

    def test_the_tests_run_like_the_release_gate(self):
        release_step = _step(self.release, STEP_NAME)
        tests_step = _step(self.tests, STEP_NAME)
        release_run = _step_block(release_step, "run")
        tests_run = _step_block(tests_step, "run")
        self.assertEqual(len(release_run), 1, release_run)
        self.assertEqual(len(tests_run), 1, tests_run)
        # 足してよいのは表示の引数（-r…）だけ。対象を絞る引数は許さない
        self.assertTrue(tests_run[0].startswith(release_run[0]),
                        (tests_run, release_run))
        extra = tests_run[0][len(release_run[0]):].split()
        self.assertTrue(all(a.startswith("-r") for a in extra), extra)
        self.assertEqual(_step_env(tests_step), _step_env(release_step))

    def test_the_tests_run_inside_the_same_shell_as_the_release(self):
        # リリースの Run tests は shell を書かず、ランナーの既定（pwsh）で走る。
        # updater.bat は呼び出し元の PSModulePath を継ぐので、shell が違うと
        # 1.3.1 で落ちた差が見えなくなる
        self.assertFalse(any(l.strip().startswith("shell:")
                             for l in _step(self.release, STEP_NAME)))
        self.assertFalse(any(l.strip().startswith("shell:")
                             for l in _step(self.tests, STEP_NAME)))
        self.assertNotRegex(self.tests, r"(?m)^\s*defaults\s*:")

    def test_the_tests_run_from_the_repository_root(self):
        # 大半のテストは sys.path.insert(0, "src") と作業ディレクトリ基準で
        # src を足す。リポジトリの直下以外から走らせると import で落ちる
        self.assertNotRegex(self.tests, r"(?m)^\s*working-directory\s*:")

    def test_the_token_is_not_handed_to_the_tests(self):
        code = [l for l in self.tests.splitlines()
                if not l.strip().startswith("#")]
        self.assertFalse([l for l in code if "GITHUB_TOKEN" in l])

    def test_a_hang_is_cut_off(self):
        self.assertRegex(self.tests, r"(?m)^\s*timeout-minutes:\s*\d+")


if __name__ == "__main__":
    unittest.main()
