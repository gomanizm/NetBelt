# -*- coding: utf-8 -*-
"""requirements.txt の PyInstaller が、onefile の既知のアドバイザリを直した版であることの検証。

PyInstaller 6.22.1 未満の onefile ブートローダは、継承した _PYI_* 環境変数を
検証しない（GHSA-9fxf-4qw3-ghmr）。昇格して動く exe を、攻撃者が用意した
環境で起動させると、攻撃者の選んだフォルダから python311.dll を読み込む。
1.3.3 までの NetBelt.exe は 6.17.0 のブートローダだった（.text が一致）。

6.22.1 と 6.22.2 は、ジャンクション・シンボリックリンク・ImDisk 上の起動を
誤って弾く不具合を抱えており、6.22.3 で直っている。そのため 6.22.3 以上を求める。

PyInstaller は pyinstaller-hooks-contrib の下限を要求する（6.22.3 は 2026.7 以上）。
片方だけ上げると pip install -r requirements.txt が ResolutionImpossible で止まり、
リリースのビルドもテストの CI も動かなくなるので、両方の固定を確かめる。
"""
import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS = os.path.join(REPO_ROOT, "requirements.txt")

MIN_PYINSTALLER = (6, 22, 3)
# 6.22.3 の Requires-Dist にある pyinstaller-hooks-contrib の下限
MIN_HOOKS_CONTRIB = (2026, 7)


def _pins():
    """requirements.txt の name==version を、正規化した名前で返す"""
    pins = {}
    with io.open(REQUIREMENTS, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*(\S+)", line)
            if m:
                pins[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return pins


def _version(text):
    return tuple(int(p) for p in re.findall(r"\d+", text))


class RequirementsPyInstallerPinTest(unittest.TestCase):

    def setUp(self):
        self.pins = _pins()

    def test_pyinstaller_has_the_onefile_environment_fix(self):
        self.assertIn("pyinstaller", self.pins, "pyinstaller が固定されていない")
        self.assertGreaterEqual(
            _version(self.pins["pyinstaller"]), MIN_PYINSTALLER,
            "pyinstaller==%s は GHSA-9fxf-4qw3-ghmr の影響版か、"
            "ジャンクション上の起動を弾く 6.22.1 / 6.22.2" % self.pins["pyinstaller"])

    def test_hooks_contrib_meets_what_pyinstaller_requires(self):
        self.assertIn("pyinstaller-hooks-contrib", self.pins,
                      "pyinstaller-hooks-contrib が固定されていない")
        self.assertGreaterEqual(
            _version(self.pins["pyinstaller-hooks-contrib"]), MIN_HOOKS_CONTRIB,
            "pyinstaller-hooks-contrib==%s では pyinstaller と依存が解決しない"
            % self.pins["pyinstaller-hooks-contrib"])


if __name__ == "__main__":
    unittest.main()
