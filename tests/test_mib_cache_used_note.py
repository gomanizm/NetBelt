"""「（キャッシュ使用）」を、本当にキャッシュを使ったときだけ出すことを検証する。

読み込み件数の行は、キャッシュを使ったかに関係なく必ず「（キャッシュ使用）」
で終わっていた。

実測（mib_cache.json を読み取り専用にして起動）:
- 「キャッシュを保存できません…次の起動でも MIB をすべて解析し直します」の
  直後に「MIBファイルから N件のOIDを読み込みました（キャッシュ使用）」が
  続き、直前の知らせを打ち消してしまう。
- 初回起動（全ファイルを解析した回）でも同じ行が出るので、起動が遅い理由を
  探している利用者に「キャッシュは効いている」と読めてしまう。

直し方: _load_or_update_mib_cache が「キャッシュをそのまま使ったか」を
覚え、使ったときだけ注記を付ける。
"""
import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

NOTE = "（キャッシュ使用）"
A_MIB = ("A-MIB DEFINITIONS ::= BEGIN\r\n"
         "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
         "aTrap OBJECT IDENTIFIER ::= { aRoot 1 }\r\n"
         "END\r\n")


class MibCacheUsedNoteTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かす
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-cachenote-")
        self.addCleanup(shutil.rmtree, self.exe_dir, True)
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(self.exe_dir, "mibs"))
        with io.open(os.path.join(self.exe_dir, "mibs", "A.my"), "w",
                     encoding="utf-8", newline="") as f:
            f.write(A_MIB)
        self.cache = os.path.join(self.exe_dir, "mib_cache.json")

    def _load(self):
        """MIBResolver を 1 つ作り、標準出力の中の読み込み件数の行を返す"""
        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MIBResolver()
        printed = out.getvalue()
        lines = [line for line in printed.splitlines()
                 if "件のOIDを読み込みました" in line]
        self.assertEqual(len(lines), 1, "読み込み件数の行が無い: %r" % printed)
        return lines[0], printed

    def test_the_first_run_does_not_claim_the_cache_was_used(self):
        """全ファイルを解析した回に、キャッシュ使用と書かないこと。"""
        line, printed = self._load()
        self.assertIn("MIBファイルを解析中", printed)
        self.assertNotIn(NOTE, line, "解析した回なのにキャッシュ使用と出る")

    def test_the_second_run_says_the_cache_was_used(self):
        """キャッシュがそのまま効いた回には、これまでどおり書くこと。"""
        self._load()
        line, printed = self._load()
        self.assertNotIn("MIBファイルを解析中", printed)
        self.assertIn(NOTE, line)

    def test_a_failed_cache_save_does_not_claim_the_cache_was_used(self):
        """保存できなかった直後に、キャッシュ使用と書かないこと。"""
        with io.open(self.cache, "w", encoding="utf-8") as f:
            f.write("{}")
        os.chmod(self.cache, stat.S_IREAD)
        self.addCleanup(os.chmod, self.cache, stat.S_IWRITE)
        line, printed = self._load()
        self.assertIn("キャッシュを保存できません", printed)
        self.assertNotIn(NOTE, line,
                         "保存できなかった知らせを打ち消している: %r" % line)


if __name__ == "__main__":
    unittest.main()
