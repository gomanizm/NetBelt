# -*- coding: utf-8 -*-
"""NetBelt.exe に Invoke を入れないことの検証。

paramiko.config は `try: import invoke` で Invoke を読むだけで、使うのは
SSHConfig の Match exec のときだけ。NetBelt は SSHConfig を使わない。
それでも PyInstaller は import を辿って Invoke の 46 モジュール（vendor の
yaml・lexicon・fluidity を含む）を exe へ入れていた。1.3.3 の配布物には
Invoke が入っているのに、THIRD-PARTY-NOTICES.txt には Invoke の項目も
BSD-2-Clause の本文も無かった（生成スクリプトが「ビルド時だけ使うもの」
として一覧から外していた）。

spec の excludes で exe から外す。実測で、外れるのは invoke と、invoke だけが
使う標準ライブラリの pty だけだった。
"""
import ast
import io
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(REPO_ROOT, "NetBelt.spec")


def _analysis_keyword(name):
    """spec の Analysis(...) に渡したキーワード引数の値（リテラル）"""
    with io.open(SPEC, encoding="utf-8") as f:
        tree = ast.parse(f.read(), SPEC)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Analysis"]
    if len(calls) != 1:
        raise AssertionError("Analysis(...) が 1 つではない: %d" % len(calls))
    for kw in calls[0].keywords:
        if kw.arg == name:
            return ast.literal_eval(kw.value)
    raise AssertionError("Analysis(...) に %s が無い" % name)


class SpecExcludesInvokeTest(unittest.TestCase):

    def test_invoke_is_excluded_from_the_exe(self):
        self.assertIn("invoke", _analysis_keyword("excludes"))

    def test_paramiko_is_still_bundled(self):
        # Invoke を外しても、SSH / SFTP に要る paramiko は明示して入れたまま
        self.assertIn("paramiko", _analysis_keyword("hiddenimports"))


if __name__ == "__main__":
    unittest.main()
