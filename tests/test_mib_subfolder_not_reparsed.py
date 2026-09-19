"""mibs/ の中のフォルダが、起動のたびの MIB の解析し直しを招かないことを検証する。

mibs/ の一覧は名前（.mib / .txt / .my）だけで選んでいたので、vendor.mib の
ような名前のフォルダも MIB ファイルとして扱っていた。フォルダは開けない
（Windows では PermissionError）ので「読めなかったファイル」になり、読めな
かったファイルは次の起動で読み直すためにキャッシュへ記録しない。その結果、
キャッシュの files と mibs/ の一覧が毎回食い違い、起動のたびに全ファイルを
解析し直していた。実測（1 ファイル＋ vendor.mib フォルダ）: 2 回目以降の起動も
毎回「MIBファイルを解析中...」と「vendor.mib エラー: [Errno 13] Permission
denied」を出し、キャッシュは使われない。40 ファイル・計 60000 定義の合成 MIB
では起動のたびに 2.8〜3.3 秒かかり続けた（直した後は 2 回目から 0.03 秒）。
フォルダは読めるようにはならないので、いつまでも止まらない。

本当に読めないファイル（アクセス権・他のアプリのロック）で毎回解析し直すのは、
読めるようになったら欠けた定義を取り戻すための設計なので変えない。ただし
標準出力は「<名前> エラー: <例外>」だけで、起動のたびに解析し直していることも、
どうすれば止まるかも分からなかった。

直し方: 一覧の段階で通常のファイル以外（フォルダなど）を外す。読めない
ファイルの標準出力は、定義が使われないこと・読めるようになるまで起動のたびに
解析し直すこと・止めるには読めるようにするか mibs フォルダから取り除くことを
伝える文言にした。
"""
import builtins
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

MIB = ("TEST-MIB DEFINITIONS ::= BEGIN\n"
       "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\n"
       "aTrap OBJECT IDENTIFIER ::= { aRoot 1 }\n"
       "END\n")


class MibSubfolderNotReparsedTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かし、
        # MIBResolver() を丸ごと作る（起動時に MIB を読む経路）
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-subfolder-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        self.mibs = os.path.join(self.exe_dir, "mibs")
        os.makedirs(self.mibs)
        self.cache_file = os.path.join(self.exe_dir, "mib_cache.json")
        with io.open(os.path.join(self.mibs, "A.my"), "w",
                     encoding="utf-8") as f:
            f.write(MIB)

    def _start(self):
        """MIBResolver を作り、(resolver, 標準出力) を返す。"""
        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resolver = MIBResolver()
        return resolver, out.getvalue()

    def _mark_cache(self):
        """キャッシュにだけある印を足す（次の起動でキャッシュが使われたかを見る）。"""
        with io.open(self.cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["mibs"]["9.9.9"] = "servedFromCache"
        with io.open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_a_folder_named_like_a_mib_does_not_force_a_reparse(self):
        """vendor.mib というフォルダがあっても、2 回目の起動でキャッシュを使うこと。"""
        os.makedirs(os.path.join(self.mibs, "vendor.mib"))
        first, _ = self._start()
        self.assertEqual(first.resolve_name("aTrap"), "1.3.6.1.4.1.1111.1")
        self._mark_cache()

        second, out = self._start()
        self.assertEqual(second.resolve_oid("9.9.9"), "servedFromCache",
                         "フォルダがあるだけで起動のたびに解析し直している")
        self.assertNotIn("vendor.mib", out)
        self.assertEqual(second.resolve_name("aTrap"), "1.3.6.1.4.1.1111.1")

    def test_an_unreadable_file_says_why_it_is_reparsed_and_how_to_stop(self):
        """読めないファイルの標準出力が、何が起きていて何をすれば止まるかを伝えること。"""
        with io.open(os.path.join(self.mibs, "B.my"), "w",
                     encoding="utf-8") as f:
            f.write("B-MIB DEFINITIONS ::= BEGIN\nEND\n")
        real_open = builtins.open

        def denied_open(file, *args, **kwargs):
            if isinstance(file, str) and os.path.basename(file) == "B.my":
                raise PermissionError(13, "Permission denied", file)
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=denied_open):
            _, out = self._start()
        lines = [line for line in out.splitlines() if "B.my" in line]
        self.assertTrue(lines, out)
        message = lines[0]
        self.assertIn("Permission denied", message)
        self.assertIn("起動のたび", message,
                      "毎回解析し直すことが伝わらない: %r" % message)
        self.assertIn("取り除", message,
                      "止め方が伝わらない: %r" % message)


if __name__ == "__main__":
    unittest.main()
