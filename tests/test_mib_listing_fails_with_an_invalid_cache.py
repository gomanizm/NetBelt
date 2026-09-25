r"""mibs/ の一覧が取れず、キャッシュも無効なときに、そのキャッシュを使わないことを検証する。

mibs/ の一覧（os.listdir）が失敗したとき（起動中の入れ替え、同期
クライアント、一覧だけを拒否する ACL）、_load_or_update_mib_cache は
読めた mib_cache.json の mibs をそのまま返していた。キャッシュが無効
（解析器の版か custom_mibs.json の指紋が違う）かどうかを見ていなかった。

実測（1.3.1、mibs/ の一覧を PermissionError にして起動）:
- 古い解析器（2026-09-22.5）が作ったキャッシュに、旧版の誤対応
  alarm=1.3.6.1.2.1.1.99（標準 system の配下）が入っていると、
  resolve_oid('1.3.6.1.2.1.1.99') が 'alarm' に戻り、読み込み件数の行に
  「（キャッシュ使用）」が付く。
- custom_mibs.json で親 acmeRoot の OID を 1111 から 2222 へ直した後も、
  resolve_oid('1.3.6.1.4.1.1111.99') が 'alarm' のまま。

利用者の決定（2026-09-20）: 無効なキャッシュは使わない。

直し方: 一覧が失敗した経路でも、キャッシュが無効なら使わずに空で返す
（_mib_cache_used も False）。知らせの 1 行も「キャッシュも古いので
使いません」に変える。有効なキャッシュはこれまでどおり使い、キャッシュの
ファイルは上書きしない。
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

A_MIB = ("A-MIB DEFINITIONS ::= BEGIN\r\n"
         "alarm OBJECT IDENTIFIER ::= { acmeRoot 99 }\r\n"
         "END\r\n")


class MibListingFailsWithAnInvalidCacheTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/・custom_mibs.json・mib_cache.json を置く凍結
        # ビルドとして動かす。場合ごとに新しい一時フォルダを使う
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-stale-cache-")
        self.addCleanup(shutil.rmtree, self.exe_dir, True)
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        self.mibs = os.path.join(self.exe_dir, "mibs")
        os.makedirs(self.mibs)
        with open(os.path.join(self.mibs, "A.my"), "wb") as f:
            f.write(A_MIB.encode("utf-8"))
        self.cache_path = os.path.join(self.exe_dir, "mib_cache.json")

    def _load(self, cache, custom):
        """キャッシュと custom_mibs.json を置き、mibs/ の一覧を拒否して読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        with open(os.path.join(self.exe_dir, "custom_mibs.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"mibs": custom}, f)
        if cache is not None:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        real_listdir = os.listdir
        mibs = self.mibs

        def denied(path="."):
            if os.path.abspath(path) == os.path.abspath(mibs):
                raise PermissionError(13, "Access is denied")
            return real_listdir(path)

        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with mock.patch("os.listdir", side_effect=denied), \
                contextlib.redirect_stdout(out):
            resolver = MIBResolver()
        return resolver, out.getvalue()

    @staticmethod
    def _older_parser_cache():
        return {"parser": "2026-09-22.5", "custom": "", "files": {},
                "mibs": {"1.3.6.1.2.1.1.99": "alarm"}}

    def test_a_cache_from_an_older_parser_is_not_used(self):
        """古い解析器のキャッシュの誤対応を、一覧できない回に戻さないこと。"""
        resolver, output = self._load(self._older_parser_cache(), {})
        self.assertNotEqual(resolver.resolve_oid("1.3.6.1.2.1.1.99"),
                            "alarm", "古い解析器の誤対応が戻っている")
        self.assertIsNone(resolver.resolve_name("alarm"))
        self.assertNotIn("（キャッシュ使用）", output,
                         "無効なキャッシュを使ったと知らせている")

    def test_a_cache_from_an_older_custom_mibs_json_is_not_used(self):
        """custom_mibs.json を直す前の結果を、一覧できない回に使わないこと。"""
        from core.mib_resolver import MIB_PARSER_VERSION
        old = json.dumps({"mibs": {"1.3.6.1.4.1.1111": "acmeRoot"}})
        resolver, output = self._load(
            {"parser": MIB_PARSER_VERSION,
             "custom": hashlib.sha1(old.encode()).hexdigest(),
             "files": {}, "mibs": {"1.3.6.1.4.1.1111.99": "alarm"}},
            {"1.3.6.1.4.1.2222": "acmeRoot"})
        self.assertNotEqual(resolver.resolve_oid("1.3.6.1.4.1.1111.99"),
                            "alarm",
                            "直す前の custom_mibs.json の結果が残っている")
        self.assertEqual(resolver.resolve_name("acmeRoot"),
                         "1.3.6.1.4.1.2222",
                         "custom_mibs.json そのものは読めているはず")
        self.assertNotIn("（キャッシュ使用）", output)

    def test_the_notice_says_the_cache_is_not_used(self):
        """一覧できなかったことと、キャッシュも使わないことを 1 行で知らせること。"""
        _, output = self._load(self._older_parser_cache(), {})
        lines = [line for line in output.splitlines()
                 if "mibs" in line and "MIBResolver" in line]
        self.assertEqual(len(lines), 1, "知らせが 1 行ではない: %r" % output)
        self.assertIn("使いません", lines[0],
                      "キャッシュを使うかのような文面のまま: %r" % lines[0])

    def test_no_cache_at_all_is_not_called_old(self):
        """キャッシュが無いだけのときに「キャッシュも古い」と書かないこと。"""
        _, output = self._load(None, {})
        lines = [line for line in output.splitlines()
                 if "mibs" in line and "MIBResolver" in line]
        self.assertEqual(len(lines), 1, "知らせが 1 行ではない: %r" % output)
        self.assertNotIn("古い", lines[0],
                         "無いキャッシュを古いと知らせている: %r" % lines[0])

    def test_the_invalid_cache_file_is_left_alone(self):
        """使わないキャッシュも、一覧できなかった回には上書きしないこと。"""
        cache = self._older_parser_cache()
        self._load(cache, {})
        with open(self.cache_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), cache)


if __name__ == "__main__":
    unittest.main()
