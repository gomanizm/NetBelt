"""mibs/ の一覧そのものが失敗しても、MIB 読み込みが落ちないことを検証する。

_mib_file_stamp の os.stat は try で囲ってあるが、その手前の
`for filename in os.listdir(mibs_dir):` は無防備だった。os.path.exists /
os.path.isdir を通った直後に mibs/ ごと消える（起動中の入れ替え、同期
クライアント、ウイルス対策の隔離）か、ACL で一覧を拒否されると、OSError が
MIBResolver.__init__ まで素通りする。

実測（os.path.isdir が True を返した直後に mibs/ を rmtree）:
- MIBResolver() が FileNotFoundError [WinError 3] を投げ、MIB を 1 件も
  読めない。内蔵の標準 MIB（sysUpTime など）まで使えなくなる。
- 画面には何も出ない。標準出力に「バックグラウンドMIB読み込みエラー」が
  1 行出るだけで、mib_loaded は True になる。

直し方: 読めない MIB ファイルと同じ形で os.listdir も囲い、理由を 1 行
残して続ける。キャッシュを読めていればその内容を返すので、一覧できない
起動でも前回の解析結果を使える。
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

A_MIB = ("A-MIB DEFINITIONS ::= BEGIN\r\n"
         "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
         "aTrap OBJECT IDENTIFIER ::= { aRoot 1 }\r\n"
         "END\r\n")


class MibDirListingFailsTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かす
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-listdir-")
        self.addCleanup(shutil.rmtree, self.exe_dir, True)
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        self.mibs = os.path.join(self.exe_dir, "mibs")
        os.makedirs(self.mibs)
        with io.open(os.path.join(self.mibs, "A.my"), "w",
                     encoding="utf-8", newline="") as f:
            f.write(A_MIB)

    def _load(self, breakage):
        """breakage（os.listdir を壊す mock の作り方）のもとで読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with breakage():
            with contextlib.redirect_stdout(out):
                resolver = MIBResolver()
        return resolver, out.getvalue()

    def _vanishing_dir(self):
        """os.path.isdir が True を返した直後に mibs/ を消す"""
        real_isdir = os.path.isdir
        mibs = self.mibs

        def isdir_then_vanish(path):
            ok = real_isdir(path)
            if ok and os.path.basename(path) == "mibs":
                shutil.rmtree(mibs, ignore_errors=True)
            return ok

        return mock.patch("os.path.isdir", side_effect=isdir_then_vanish)

    def _denied_listing(self):
        """mibs/ の一覧だけが PermissionError になる（ACL で拒否）"""
        real_listdir = os.listdir
        mibs = self.mibs

        def listdir_denied(path="."):
            if os.path.abspath(path) == os.path.abspath(mibs):
                raise PermissionError(13, "Access is denied")
            return real_listdir(path)

        return mock.patch("os.listdir", side_effect=listdir_denied)

    def test_the_builtin_standard_mibs_still_load(self):
        """一覧できなくても、内蔵の標準 MIB で起動できること。"""
        resolver, _ = self._load(self._vanishing_dir)
        self.assertEqual(resolver.resolve_oid("1.3.6.1.2.1.1.3.0"),
                         "sysUpTime",
                         "標準 MIB まで読めていない")
        self.assertEqual(resolver.resolve_name("enterprises"), "1.3.6.1.4.1")

    def test_the_reason_is_printed_once(self):
        """何が起きたかを、mibs を名指しして 1 行残すこと。"""
        _, printed = self._load(self._vanishing_dir)
        lines = [line for line in printed.splitlines()
                 if "mibs" in line and "MIBResolver" in line]
        self.assertEqual(len(lines), 1,
                         "一覧できなかったことが分からない: %r" % printed)

    def test_a_denied_listing_falls_back_to_the_cache(self):
        """一覧を拒否されても、前回の解析結果が残っていれば使えること。"""
        from core.mib_resolver import MIBResolver
        with contextlib.redirect_stdout(io.StringIO()):
            first = MIBResolver()
        self.assertEqual(first.resolve_name("aTrap"), "1.3.6.1.4.1.1111.1",
                         "前提が崩れている（1 回目で読めていない）")
        resolver, printed = self._load(self._denied_listing)
        self.assertEqual(resolver.resolve_name("aTrap"),
                         "1.3.6.1.4.1.1111.1",
                         "キャッシュがあるのに使われていない: %r" % printed)

    def test_the_cache_file_is_left_alone(self):
        """一覧できなかった回に、キャッシュを空で上書きしないこと。"""
        import json
        from core.mib_resolver import MIBResolver
        with contextlib.redirect_stdout(io.StringIO()):
            MIBResolver()
        cache = os.path.join(self.exe_dir, "mib_cache.json")
        with io.open(cache, encoding="utf-8") as f:
            before = json.load(f)
        self._load(self._denied_listing)
        with io.open(cache, encoding="utf-8") as f:
            self.assertEqual(json.load(f), before)


if __name__ == "__main__":
    unittest.main()
