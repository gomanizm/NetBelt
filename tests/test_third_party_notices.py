# -*- coding: utf-8 -*-
"""THIRD-PARTY-NOTICES.txt の生成がロケールに影響されないことを確認する。

生成物には日本語の見出しが入る。sys.stdout の encoding はロケール依存で、
リダイレクト時は locale.getpreferredencoding() が使われるため、
英語ロケールの Windows では cp1252 になり UnicodeEncodeError で落ちる。

開発機が日本語 Windows だとこの失敗は再現せず、CI（en-US の
windows-latest）で初めて表面化した。ロケールを明示的に変えて検証する。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "gen_third_party_notices.py"


def run_with_stdout_encoding(encoding):
    """stdout の encoding を指定してスクリプトを実行し、生の bytes を返す。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = encoding
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class TestThirdPartyNotices(unittest.TestCase):

    def test_generates_utf8_under_western_locale(self):
        """cp1252（英語ロケール相当）でも UTF-8 の生成物が得られる。"""
        proc = run_with_stdout_encoding("cp1252")

        self.assertEqual(
            proc.returncode, 0,
            "cp1252 環境で生成が失敗した:\n" + proc.stderr.decode("utf-8", "replace"))

        # bytes が UTF-8 として解釈できること（cp1252 で書かれていたら失敗する）
        text = proc.stdout.decode("utf-8")
        self.assertIn("NetBelt サードパーティ ライセンス表示", text)

    def test_lists_the_gpl_relevant_packages(self):
        """GPL の判断に効くパッケージが表示に含まれる。"""
        proc = run_with_stdout_encoding("utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        text = proc.stdout.decode("utf-8")

        for pkg in ("PyQt6", "paramiko"):
            self.assertIn(pkg, text, "%s が一覧に無い" % pkg)

        # dist-info の記載ではなく、本文を読んで確定させた値であること
        self.assertIn("LGPL-2.1-or-later", text)


if __name__ == "__main__":
    unittest.main()
