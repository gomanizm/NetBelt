"""キャッシュを保存できないときと、ファイルとして開けない名前を外したときの知らせを検証する。

mib_cache.json を書けないとき、標準出力は「キャッシュ保存エラー: <例外>」
だけだった。読めない MIB ファイルのときに整えた文言（何が起きているか・
次の起動でもやり直すこと・止める方法）と揃っていない。実測（mib_cache.json
という名前のフォルダを作って書き込みを失敗させ、3 回起動する）: 3 回とも
「MIBファイルを解析中...」から全 MIB を解析し直し、キャッシュは作られず、
出るのは例外の文言だけ。書き込めない場所へ Portable 版を置いた利用者は、
起動が毎回遅い理由も止め方も分からない。

もう 1 つ、mibs/ の一覧から通常のファイル以外を外す判定は os.path.isfile
だけだった。os.path.isfile は壊れたリンクや MAX_PATH を超えるパスでも
False を返すので、名前は .mib / .my / .txt なのに黙って読み込みから外れる
経路ができていた（フォルダのときは外して黙るのが正しいが、それ以外は
利用者に見えない）。

直し方: キャッシュ保存の失敗は、読めない MIB ファイルと同じ考えの文言
（何が起きているか・次の起動でもやり直すこと・止める方法）にした。一覧
から外す判定はフォルダとそれ以外に分け、フォルダ以外で外したものは名前を
1 行出す（フォルダは今までどおり黙って外す。起動のたびに出しても
利用者にできることが無いため）。
"""
import contextlib
import io
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


class MibCacheUnwritableMessageTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かし、
        # MIBResolver() を丸ごと作る（起動時に MIB を読む経路）
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-unwritable-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        self.mibs = os.path.join(self.exe_dir, "mibs")
        os.makedirs(self.mibs)
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

    def test_an_unwritable_cache_says_why_every_start_reparses(self):
        """保存できないときの知らせが、毎回やり直すことと止め方を伝えること。"""
        # mib_cache.json という名前のフォルダを置いて書き込みを失敗させる
        os.makedirs(os.path.join(self.exe_dir, "mib_cache.json"))
        for attempt in (1, 2, 3):
            resolver, out = self._start()
            with self.subTest(attempt=attempt):
                self.assertEqual(resolver.resolve_name("aTrap"),
                                 "1.3.6.1.4.1.1111.1")
                self.assertIn("MIBファイルを解析中", out,
                              "保存できないので毎回解析し直すはず")
                lines = [line for line in out.splitlines()
                         if "キャッシュ" in line and "更新しました" not in line]
                self.assertTrue(lines, out)
                message = lines[0]
                self.assertIn("起動", message,
                              "次の起動でもやり直すことが伝わらない: %r"
                              % message)
                self.assertIn("解析し直", message,
                              "何が起きているかが伝わらない: %r" % message)
                self.assertIn("書き込", message,
                              "止め方が伝わらない: %r" % message)

    def test_a_name_that_is_not_a_file_is_named_once(self):
        """フォルダ以外の理由で一覧から外した名前が、標準出力に出ること。"""
        with io.open(os.path.join(self.mibs, "ghost.my"), "w",
                     encoding="utf-8") as f:
            f.write("GHOST-MIB DEFINITIONS ::= BEGIN\nEND\n")
        real_isfile = os.path.isfile

        def not_a_file(path):
            # 壊れたリンクや MAX_PATH 超えで isfile が False になる形
            if os.path.basename(path) == "ghost.my":
                return False
            return real_isfile(path)

        with mock.patch("os.path.isfile", side_effect=not_a_file):
            resolver, out = self._start()
        self.assertEqual(resolver.resolve_name("aTrap"), "1.3.6.1.4.1.1111.1")
        self.assertIn("ghost.my", out,
                      "名前が合っているのに黙って外れている: %r" % out)


if __name__ == "__main__":
    unittest.main()
