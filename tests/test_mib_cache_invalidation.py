"""mib_cache.json が custom_mibs.json の変更と解析器の更新で作り直される
ことを検証する。

キャッシュの鍵は mibs/ 配下のファイルの mtime だけだった。custom_mibs.json
で親の OID を直して再起動しても、変わっていない MIB ファイルから来た子は
古い親の下に残る（実測: myCompanyRoot を 12345.9.9 へ直したあとも child は
12345.1.1.7 のまま、12345.9.9.7 は 'myCompanyRoot.7'）。解析器の版も
記録していないので、古い（バグのある）解析器が作ったキャッシュがアプリを
更新したあともそのまま使われる（実測: 改ざんした mibs が再解析なしで
返る）。

custom_mibs.json の内容のハッシュと解析器の版をキャッシュに入れ、どちらか
が違えば作り直す。
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

SMI_HEAD = "TEST-SMI DEFINITIONS ::= BEGIN\n"
SMI_TAIL = "\nEND\n"


class MibCacheInvalidationTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/・custom_mibs.json・mib_cache.json を置く凍結
        # ビルドとして動かす。リポジトリ直下の custom_mibs.json に
        # 引きずられないようにするため。
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-cache-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(self.exe_dir, "mibs"))
        self.cache_file = os.path.join(self.exe_dir, "mib_cache.json")

    def _write_mib(self, name, body):
        with io.open(os.path.join(self.exe_dir, "mibs", name), "w",
                     encoding="utf-8") as f:
            f.write(SMI_HEAD + body + SMI_TAIL)

    def _write_custom(self, mibs):
        with io.open(os.path.join(self.exe_dir, "custom_mibs.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"mibs": mibs}, f)

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def _tamper_cache(self, **changes):
        """キャッシュの一部を書き換える（files は触らない）。"""
        with io.open(self.cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        with io.open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_changing_a_parent_in_custom_mibs_rebuilds_the_children(self):
        """custom_mibs.json の親を直したら、子も新しい親の下へ移ること。"""
        self._write_custom({"1.3.6.1.4.1.12345.1.1": "myCompanyRoot"})
        self._write_mib("X.my",
                        "child OBJECT IDENTIFIER ::= { myCompanyRoot 7 }\n")
        first = self._resolver()
        self.assertEqual(first.resolve_name("child"), "1.3.6.1.4.1.12345.1.1.7")

        self._write_custom({"1.3.6.1.4.1.12345.9.9": "myCompanyRoot"})
        second = self._resolver()
        self.assertEqual(second.resolve_name("myCompanyRoot"),
                         "1.3.6.1.4.1.12345.9.9")
        self.assertEqual(second.resolve_name("child"),
                         "1.3.6.1.4.1.12345.9.9.7",
                         "子が古い親の下に残っている")
        self.assertEqual(second.resolve_oid("1.3.6.1.4.1.12345.9.9.7"), "child")
        self.assertNotEqual(second.resolve_oid("1.3.6.1.4.1.12345.1.1.7"),
                            "child", "古い OID がまだ child に解決される")

    def test_a_cache_without_a_parser_version_is_rebuilt(self):
        """版の無い（古い解析器の）キャッシュは使わず作り直すこと。"""
        self._write_mib("X.my",
                        "xRoot OBJECT IDENTIFIER ::= { enterprises 5555 }\n")
        self._resolver()
        self._tamper_cache(parser=None, mibs={"9.9.9": "bogusFromOldParser"})

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("9.9.9"), "9.9.9",
                         "古いキャッシュがそのまま使われている")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.5555"), "xRoot")

    def test_a_cache_from_another_parser_version_is_rebuilt(self):
        """記録された版が今の解析器と違えば作り直すこと。"""
        from core import mib_resolver
        self._write_mib("X.my",
                        "xRoot OBJECT IDENTIFIER ::= { enterprises 5555 }\n")
        self._resolver()
        with io.open(self.cache_file, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f).get("parser"),
                             mib_resolver.MIB_PARSER_VERSION,
                             "キャッシュに解析器の版が入っていない")
        self._tamper_cache(parser="0-older", mibs={"9.9.9": "bogus"})

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("9.9.9"), "9.9.9")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.5555"), "xRoot")

    def test_an_unchanged_setup_still_reuses_the_cache(self):
        """何も変わっていなければ、これまでどおりキャッシュを読むこと。"""
        self._write_custom({"1.3.6.1.4.1.12345.1.1": "myCompanyRoot"})
        self._write_mib("X.my",
                        "child OBJECT IDENTIFIER ::= { myCompanyRoot 7 }\n")
        self._resolver()
        self._tamper_cache(mibs={"9.9.9": "servedFromCache"})

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("9.9.9"), "servedFromCache",
                         "変更が無いのにキャッシュを作り直している")

    def test_the_custom_fingerprint_is_the_file_content_not_its_mtime(self):
        """同じ mtime でも中身が違えば作り直すこと（コピーで mtime が
        保たれる／同じ秒に書き直す場合）。"""
        self._write_custom({"1.3.6.1.4.1.12345.1.1": "myCompanyRoot"})
        self._write_mib("X.my",
                        "child OBJECT IDENTIFIER ::= { myCompanyRoot 7 }\n")
        self._resolver()
        custom = os.path.join(self.exe_dir, "custom_mibs.json")
        stat = os.stat(custom)
        self._write_custom({"1.3.6.1.4.1.12345.9.9": "myCompanyRoot"})
        os.utime(custom, (stat.st_atime, stat.st_mtime))
        with io.open(self.cache_file, "r", encoding="utf-8") as f:
            recorded = json.load(f).get("custom")
        with io.open(custom, "rb") as f:
            now = hashlib.sha1(f.read()).hexdigest()
        self.assertNotEqual(recorded, now, "指紋が中身を見ていない")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("child"),
                         "1.3.6.1.4.1.12345.9.9.7")


if __name__ == "__main__":
    unittest.main()
