# -*- coding: utf-8 -*-
"""THIRD-PARTY-NOTICES.txt が、exe に実際に入るものを載せることの検証。

生成スクリプトは BUILD_ONLY を「実行時には同梱されない、開発・ビルド時のみ
使うもの」として一覧から外していた。ところが 1.3.3 の NetBelt.exe には
そのうち 3 つが入っていた。

- setuptools（vendored の部品を含めて 179 モジュール）と packaging:
  urllib3 の任意の import（backports.zstd）を PyInstaller が setuptools の
  vendored 版へ別名付けするので入る。pyi_rth_setuptools が起動のたびに
  import している。exe の挙動を変えないため同梱のままにし、通知に載せる。
- invoke: paramiko.config が try で import するだけで使わない。
  NetBelt.spec の excludes で exe から外した（test_spec_excludes_invoke）。

setuptools は取り込んだ部品の dist-info（setuptools/_vendor/*.dist-info）を
持っており、exe にはその部品のコードが入る。部品のライセンス本文も、推測せず
実物から読んで載せる。
"""
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "tools", "gen_third_party_notices.py")
SPEC = os.path.join(REPO_ROOT, "NetBelt.spec")
SEP = "=" * 78


def _generate(extra_path=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_path:
        env["PYTHONPATH"] = extra_path
    proc = subprocess.run([sys.executable, SCRIPT], cwd=REPO_ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.decode("utf-8", "replace"))
    return proc.stdout.decode("utf-8")


def _normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _listed(text):
    """一覧に載った名前（正規化済み）"""
    lines = text.splitlines()
    start = lines.index("一覧") + 2
    names = set()
    for line in lines[start:]:
        if not line.strip():
            break
        names.add(_normalize(line.split()[0]))
    return names


def _section(text, name):
    """name の節（見出しから次の節の見出しまで）"""
    lines = text.splitlines()
    starts = [i for i in range(len(lines) - 2)
              if lines[i] == SEP and lines[i + 2].startswith("ライセンス: ")]
    for n, i in enumerate(starts):
        if _normalize(lines[i + 1].split()[0]) == _normalize(name):
            end = starts[n + 1] if n + 1 < len(starts) else len(lines)
            return "\n".join(lines[i:end])
    raise AssertionError("%s の節が無い" % name)


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_third_party_notices", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec_excludes():
    import ast
    with io.open(SPEC, encoding="utf-8") as f:
        tree = ast.parse(f.read(), SPEC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Analysis":
            for kw in node.keywords:
                if kw.arg == "excludes":
                    return set(ast.literal_eval(kw.value))
    raise AssertionError("NetBelt.spec の Analysis に excludes が無い")


class NoticesFollowTheBundleTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _generate()
        cls.listed = _listed(cls.text)

    def test_setuptools_and_packaging_are_listed(self):
        # どちらも requirements.txt で固定しており、ビルド環境に必ずある
        for name in ("setuptools", "packaging"):
            with self.subTest(name=name):
                self.assertIn(name, self.listed)

    def test_invoke_is_not_listed(self):
        # requirements.txt にあり（paramiko の依存）、ビルド環境にもあるが exe に入らない
        self.assertNotIn("invoke", self.listed)

    def test_build_tools_are_still_left_out(self):
        for name in ("pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.listed)

    def test_what_is_left_out_as_excluded_is_excluded_by_the_spec(self):
        # 通知から外すものと、spec の excludes は手で揃えている。spec から
        # 外し忘れると、exe に入るのに通知に載らないものができる
        excluded = getattr(_load_generator(), "EXCLUDED_FROM_EXE", None)
        self.assertIsNotNone(excluded, "spec で exe から外したものの集合が無い")
        self.assertIn("invoke", excluded)
        self.assertLessEqual(set(excluded), _spec_excludes())


class VendoredLicencesTest(unittest.TestCase):
    """取り込んだ部品の dist-info にあるライセンス本文を、親の節に載せること"""

    PARENT = "netbelt_fake_parent"
    VENDORED = "netbelt_fake_parent/_vendor/fakevendored-2.0.dist-info"

    def setUp(self):
        self.site = tempfile.mkdtemp(prefix="netbelt-notices-")
        self.addCleanup(shutil.rmtree, self.site, True)
        files = {
            "%s-1.0.dist-info/METADATA" % self.PARENT:
                "Metadata-Version: 2.1\nName: netbelt-fake-parent\nVersion: 1.0\n"
                "License: MIT\n",
            "%s-1.0.dist-info/LICENSE" % self.PARENT: "PARENT-OWN-LICENSE-TEXT\n",
            "%s/__init__.py" % self.PARENT: "",
            "%s/_vendor/__init__.py" % self.PARENT: "",
            "%s/_vendor/fakevendored/__init__.py" % self.PARENT: "",
            self.VENDORED + "/METADATA":
                "Metadata-Version: 2.1\nName: fakevendored\nVersion: 2.0\n",
            self.VENDORED + "/LICENSE": "VENDORED-PART-LICENSE-TEXT\nsecond line\n",
            self.VENDORED + "/licenses/NOTICE": "VENDORED-PART-NOTICE-TEXT\n",
            "%s/_vendor/fakevendored/LICENSE" % self.PARENT: "STRAY-VENDORED-LICENSE-TEXT\n",
        }
        record = "%s-1.0.dist-info/RECORD" % self.PARENT
        files[record] = "".join("%s,,\n" % p for p in sorted(files)) + record + ",,\n"
        for path, body in files.items():
            full = os.path.join(self.site, *path.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with io.open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
        self.text = _generate(self.site)

    def test_the_parent_is_listed_with_its_own_licence(self):
        self.assertIn("netbelt-fake-parent", _listed(self.text))
        self.assertIn("PARENT-OWN-LICENSE-TEXT", _section(self.text, "netbelt-fake-parent"))

    def test_the_vendored_licence_texts_are_in_the_parent_section(self):
        section = _section(self.text, "netbelt-fake-parent")
        self.assertIn("--- %s/LICENSE ---" % self.VENDORED, section)
        self.assertIn("VENDORED-PART-LICENSE-TEXT\nsecond line", section)
        self.assertIn("--- %s/licenses/NOTICE ---" % self.VENDORED, section)
        self.assertIn("VENDORED-PART-NOTICE-TEXT", section)

    def test_licence_files_outside_the_vendored_dist_info_are_not_read(self):
        # 読むのは入れ子の dist-info の中だけ。部品のディレクトリに置かれた本文
        # （pip/_vendor/*/LICENSE など）まで拾うと、exe に入らない pip の節が膨らむ
        self.assertNotIn("STRAY-VENDORED-LICENSE-TEXT", self.text)

    def test_the_vendored_part_is_not_listed_as_a_package_of_its_own(self):
        # 部品の dist-info は site-packages の直下に無いので、別の項目にはならない
        self.assertNotIn("fakevendored", _listed(self.text))


def _install(site, files, unrecorded=None):
    """files（パス → 本文）を site に書き、最初の dist-info に RECORD を置く。

    unrecorded は書くが、RECORD には挙げない。
    """
    info = min(p.split("/")[0] for p in files if p.split("/")[0].endswith(".dist-info"))
    record = info + "/RECORD"
    body = "".join("%s,,\n" % p for p in sorted(files)) + record + ",,\n"
    for path, text in list(files.items()) + list((unrecorded or {}).items()) + [(record, body)]:
        full = os.path.join(site, *path.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with io.open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


class NoticeFilesTest(unittest.TestCase):
    """dist-info の外に置かれた表示（NOTICE 系）も、RECORD から実物を読んで節に載せること。

    exe に入る setuptools.config._validate_pyproject は、fastjsonschema
    （BSD-3-Clause）と validate-pyproject（MPL-2.0）に由来するコード。その表示は
    setuptools/config/NOTICE と setuptools/config/_validate_pyproject/NOTICE にあり、
    dist-info の中にも vendor の下にも無いので、通知から落ちていた。
    """

    PARENT = "netbelt_fake_noticed"

    def setUp(self):
        self.site = tempfile.mkdtemp(prefix="netbelt-notices-")
        self.addCleanup(shutil.rmtree, self.site, True)
        p = self.PARENT
        _install(self.site, {
            "%s-1.0.dist-info/METADATA" % p:
                "Metadata-Version: 2.1\nName: netbelt-fake-noticed\nVersion: 1.0\n"
                "License: MIT\n",
            "%s-1.0.dist-info/LICENSE" % p: "NOTICED-OWN-LICENSE-TEXT\n",
            "%s/__init__.py" % p: "",
            "%s/config/__init__.py" % p: "",
            "%s/config/NOTICE" % p: "CONFIG-NOTICE-TEXT\nfrom another project\n",
            "%s/config/_generated/NOTICE.txt" % p: "GENERATED-NOTICE-TEXT\n",
            "%s/notice.py" % p: "NOT_A_NOTICE = 1\n",
        }, unrecorded={"%s/stray/NOTICE" % p: "UNRECORDED-NOTICE-TEXT\n"})
        self.text = _generate(self.site)

    def test_notice_files_outside_the_dist_info_are_in_the_section(self):
        section = _section(self.text, "netbelt-fake-noticed")
        self.assertIn("NOTICED-OWN-LICENSE-TEXT", section)
        for path, body in (("config/NOTICE", "CONFIG-NOTICE-TEXT\nfrom another project"),
                           ("config/_generated/NOTICE.txt", "GENERATED-NOTICE-TEXT")):
            with self.subTest(path=path):
                self.assertIn("--- %s/%s ---" % (self.PARENT, path), section)
                self.assertIn(body, section)

    def test_only_notice_files_named_in_record_are_read(self):
        # 推測せず、RECORD に挙がった実物だけを読む。notice で始まるモジュールも読まない
        self.assertNotIn("UNRECORDED-NOTICE-TEXT", self.text)
        self.assertNotIn("NOT_A_NOTICE", self.text)


class BuildEnvironmentNoticesTest(unittest.TestCase):
    """ビルド環境（requirements.txt で固定）から作った通知が、実物どおりであること"""

    @classmethod
    def setUpClass(cls):
        cls.text = _generate()

    @staticmethod
    def _flat(text):
        return " ".join(text.split())

    def _installed(self, dist, pattern):
        """dist の RECORD のうち、パスが pattern に合うファイルの本文"""
        import importlib.metadata as md
        found = {}
        for f in md.distribution(dist).files or []:
            if re.fullmatch(pattern, f.as_posix()):
                with io.open(f.locate(), encoding="utf-8") as fh:
                    found[f.as_posix()] = fh.read()
        return found

    def test_setuptools_config_notices_are_in_the_setuptools_section(self):
        section = self._flat(_section(self.text, "setuptools"))
        found = self._installed("setuptools", r"setuptools/config/(.+/)?NOTICE")
        self.assertEqual(set(found), {"setuptools/config/NOTICE",
                                      "setuptools/config/_validate_pyproject/NOTICE"})
        for path, body in found.items():
            with self.subTest(path=path):
                self.assertIn("--- %s ---" % path, section)
                self.assertIn(self._flat(body), section)

    def test_packaging_is_listed_under_both_licences(self):
        # 最初の classifier（Apache Software License）だけでは、BSD の側が落ちる
        lines = self.text.splitlines()
        rows = lines[lines.index("一覧") + 2:]
        row = next(l for l in rows[:rows.index("")] if l.split()[0] == "packaging")
        self.assertEqual(row.split()[2:], ["Apache-2.0", "OR", "BSD-2-Clause"])
        self.assertIn("ライセンス: Apache-2.0 OR BSD-2-Clause", _section(self.text, "packaging"))

    def test_the_packaging_section_has_all_three_licence_texts(self):
        section = self._flat(_section(self.text, "packaging"))
        found = {p.rsplit("/", 1)[1]: b for p, b in
                 self._installed("packaging", r"packaging-[^/]+\.dist-info/licenses/.+").items()}
        self.assertEqual(set(found), {"LICENSE", "LICENSE.APACHE", "LICENSE.BSD"})
        for name, body in found.items():
            with self.subTest(name=name):
                self.assertIn(self._flat(body), section)
        # 一覧の表記の根拠。LICENSE は 2 つの本文の二者択一と書いている
        self.assertIn("*either* of the licenses found in LICENSE.APACHE or LICENSE.BSD",
                      self._flat(found["LICENSE"]))


if __name__ == "__main__":
    unittest.main()
