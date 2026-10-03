# -*- coding: utf-8 -*-
"""同梱物と通知の照合を、リリースのビルドで実際に走らせることの検証。

tools/check_bundled_notices.py の判定は test_check_bundled_notices が見る。
ここでは、コマンドとしての終了コード（CI の手順を止めるかどうか）と、
build-release.yml が Build executable の直後にこれを走らせることを確かめる。
exe は作らず、exe を読む部分だけを差し替える。
"""
import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "tools", "check_bundled_notices.py")
RELEASE = os.path.join(REPO_ROOT, ".github", "workflows", "build-release.yml")
SEP = "=" * 78
STEP = "Check bundled notices"


def _load():
    spec = importlib.util.spec_from_file_location("check_bundled_notices", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CommandTest(unittest.TestCase):

    def setUp(self):
        self.tool = _load()
        self.work = tempfile.mkdtemp(prefix="netbelt-check-notices-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.src = os.path.join(self.work, "src")
        os.makedirs(os.path.join(self.src, "core"))
        os.makedirs(os.path.join(self.src, "__pycache__"))
        for name in ("main.py", "__version__.py"):
            io.open(os.path.join(self.src, name), "w").close()
        self.exe = os.path.join(self.work, "NetBelt.exe")
        io.open(self.exe, "wb").close()

    def _notices(self, *names):
        path = os.path.join(self.work, "THIRD-PARTY-NOTICES.txt")
        lines = ["NetBelt サードパーティ ライセンス表示", SEP, "", "-" * 78, "一覧", "-" * 78]
        lines += ["  %-28s 1.0          MIT" % n for n in names] + [""]
        for n in names:
            lines += [SEP, "%s 1.0" % n, "ライセンス: MIT", SEP, "", "TEXT", ""]
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _run(self, notices, modules):
        contents = ([("s", "main"), ("s", "pyi_rth_inspect")], modules)
        out = io.StringIO()
        with mock.patch.object(self.tool, "read_exe", return_value=contents), \
                contextlib.redirect_stdout(out):
            code = self.tool.main(["--exe", self.exe, "--notices", notices, "--src", self.src])
        return code, out.getvalue()

    def test_a_bundled_package_missing_from_the_notices_stops_the_build(self):
        # invoke は requirements.txt にあり、ビルド環境に必ず入っている
        code, out = self._run(self._notices("paramiko"),
                              ["core.ssh_connection", "paramiko.config", "invoke.runners"])
        self.assertEqual(code, 1, out)
        self.assertIn("invoke", out)

    def test_a_matching_bundle_passes(self):
        code, out = self._run(self._notices("paramiko"),
                              ["core.ssh_connection", "paramiko.config", "os.path"])
        self.assertEqual(code, 0, out)

    def test_a_name_without_a_distribution_stops_the_build(self):
        code, out = self._run(self._notices("paramiko"), ["zz_mystery_pkg.mod"])
        self.assertEqual(code, 1, out)
        self.assertIn("割り出せない: zz_mystery_pkg", out)
        self.assertNotIn("通知の一覧に無い", out)
        self.assertNotIn("部品の本文が無い", out)

    def test_a_vendored_part_without_its_licence_stops_the_build(self):
        # 通知の setuptools の節は TEXT だけで、ビルド環境の zipp の本文を含まない
        code, out = self._run(self._notices("setuptools"),
                              ["setuptools.dist", "setuptools._vendor.zipp"])
        self.assertEqual(code, 1, out)
        self.assertIn("部品の本文が無い: setuptools/_vendor/zipp", out)
        self.assertNotIn("通知の一覧に無い", out)
        self.assertNotIn("割り出せない", out)

    def test_missing_inputs_cannot_be_checked(self):
        notices = self._notices("paramiko")
        for argv in (["--exe", os.path.join(self.work, "none.exe"), "--notices", notices],
                     ["--exe", self.exe, "--notices", os.path.join(self.work, "none.txt")]):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.tool.main(argv + ["--src", self.src]), 2)

    def test_a_failure_while_checking_cannot_be_checked_either(self):
        # 部品のライセンスファイルを読めないなど、照合の途中の失敗も 2。
        # 1（通知に無いものがある）と区別する
        with mock.patch.object(self.tool, "installed_licences",
                               side_effect=PermissionError("denied")):
            code, out = self._run(self._notices("setuptools"),
                                  ["setuptools.dist", "setuptools._vendor.zipp"])
        self.assertEqual(code, 2, out)
        self.assertIn("照合できません", out)

    def test_own_names_come_from_the_source_folder(self):
        self.assertEqual(self.tool.own_names(self.src), {"main", "__version__", "core"})

    def test_vendored_licences_are_read_from_the_build_environment(self):
        # setuptools は requirements.txt で固定しており、_vendor に zipp の dist-info を持つ
        found = self.tool.installed_licences("setuptools", ("setuptools", "_vendor", "zipp"))
        self.assertTrue(found)
        for path, body in found:
            self.assertRegex(path, r"^setuptools/_vendor/zipp-[^/]+\.dist-info/")
            self.assertTrue(body.strip())


class WesternLocaleTest(unittest.TestCase):
    """英語ロケール（cp1252）のランナーでも、日本語の出力で落ちないこと。

    windows-latest の標準出力は cp1252 で、日本語を書くと UnicodeEncodeError で
    終了コード 1 になる。照合の結果に関係なく、毎回のリリースが止まってしまう。
    """

    def _run(self, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp1252"
        proc = subprocess.run([sys.executable, TOOL] + list(args), cwd=REPO_ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8", "replace")

    def test_the_help_is_printed(self):
        code, out, err = self._run("--help")
        self.assertEqual(code, 0, err)
        self.assertIn("exe の同梱物と通知の一覧を突き合わせる", out)

    def test_an_input_that_cannot_be_read_is_reported_as_such(self):
        work = tempfile.mkdtemp(prefix="netbelt-check-notices-")
        self.addCleanup(shutil.rmtree, work, True)
        code, out, err = self._run("--notices", os.path.join(work, "none.txt"))
        self.assertEqual(code, 2, err)
        self.assertIn("照合できません", out)


class ReleaseWorkflowTest(unittest.TestCase):

    def setUp(self):
        with io.open(RELEASE, encoding="utf-8") as f:
            self.text = f.read()
        self.names = re.findall(r"(?m)^\s*- name:\s*(.+?)\s*$", self.text)

    def _step(self, name):
        lines = self.text.splitlines()
        start = next(i for i, l in enumerate(lines) if l.strip() == "- name: %s" % name)
        indent = len(lines[start]) - len(lines[start].lstrip())
        body = [lines[start]]
        for line in lines[start + 1:]:
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            body.append(line)
        return body

    def test_the_check_runs_right_after_the_build(self):
        self.assertIn(STEP, self.names)
        self.assertEqual(self.names[self.names.index("Build executable") + 1], STEP)
        # 照合する通知は、同じジョブで生成し直したもの
        self.assertLess(self.names.index("Generate third-party notices"),
                        self.names.index("Build executable"))

    def test_a_failed_check_stops_the_release(self):
        step = [l.strip() for l in self._step(STEP) if l.strip() and not l.strip().startswith("#")]
        self.assertEqual(step, ["- name: %s" % STEP, "run: |",
                                "python tools/check_bundled_notices.py"])


class NoticeFilesCommandTest(unittest.TestCase):
    """表示（NOTICE 系）の照合で、ビルド環境の RECORD にある実物を読むこと。

    setuptools は requirements.txt で固定しており、exe に入る
    setuptools.config._validate_pyproject（fastjsonschema と validate-pyproject に
    由来するコード）の表示を、setuptools/config の下に 2 つ持つ。
    """

    setUp = CommandTest.setUp
    _notices = CommandTest._notices
    _run = CommandTest._run
    MODULES = ["setuptools.dist", "setuptools.config._validate_pyproject.formats"]

    def test_a_notice_missing_from_the_section_stops_the_build(self):
        code, out = self._run(self._notices("setuptools"), self.MODULES)
        self.assertEqual(code, 1, out)
        for path in ("setuptools/config/NOTICE", "setuptools/config/_validate_pyproject/NOTICE"):
            self.assertIn("%s の本文が setuptools の節にありません" % path, out)
        self.assertNotIn("通知の一覧に無い", out)
        self.assertNotIn("割り出せない", out)

    def test_the_notices_in_the_section_pass(self):
        notices = self._notices("setuptools")
        with io.open(notices, "a", encoding="utf-8") as f:  # 最後の節（setuptools）に足す
            for path, body in self.tool.installed_notices("setuptools"):
                f.write("\n--- %s ---\n%s\n" % (path, body))
        code, out = self._run(notices, self.MODULES)
        self.assertEqual(code, 0, out)

    def test_the_names_match_those_the_generator_reads(self):
        path = os.path.join(REPO_ROOT, "tools", "gen_third_party_notices.py")
        spec = importlib.util.spec_from_file_location("gen_third_party_notices", path)
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        self.assertEqual(self.tool.NOTICE_NAME.pattern, gen.NOTICE_NAME.pattern)
        self.assertEqual(self.tool.NOTICE_NAME.flags, gen.NOTICE_NAME.flags)

    def test_only_files_named_as_notices_in_record_are_read(self):
        # generator と同じく、名前が NOTICE_NAME に完全一致する RECORD の実物だけを読む。
        # notice.py や NOTICE-3rdparty.txt のような、NOTICE で始まるだけのものは読まない
        site = os.path.join(self.work, "site")
        top = "netbelt_fake_cli_noticed"
        info = top + "-1.0.dist-info"
        expected = {
            info + "/licenses/NOTICE": "DIST-INFO-NOTICE-TEXT\n",
            top + "/config/NOTICE": "CONFIG-NOTICE-TEXT\n",
            top + "/config/sub/notices.md": "SUB-NOTICES-TEXT\n",
        }
        files = dict(expected)
        files.update({
            info + "/METADATA":
                "Metadata-Version: 2.1\nName: netbelt-fake-cli-noticed\nVersion: 1.0\n",
            top + "/__init__.py": "",
            top + "/notice.py": "NOT_A_NOTICE = 1\n",
            top + "/NOTICE-3rdparty.txt": "PREFIX-ONLY-TEXT\n",
        })
        record = info + "/RECORD"
        files[record] = "".join("%s,,\n" % p for p in sorted(files)) + record + ",,\n"
        files[top + "/stray/NOTICE"] = "UNRECORDED-NOTICE-TEXT\n"  # RECORD には挙げない
        for path, body in files.items():
            full = os.path.join(site, *path.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with io.open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
        with mock.patch.object(sys, "path", [site] + sys.path):
            found = dict(self.tool.installed_notices("netbelt-fake-cli-noticed"))
        self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
