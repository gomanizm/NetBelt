"""mibs/ に置いた大文字拡張子の MIB ファイルも読むことを検証する。

拡張子の判定が filename.endswith(('.mib', '.txt', '.my')) で、Windows は
ファイル名の大小を保持するため、CASE.MIB のように大文字で配布された MIB
は無言で無視されていた（実測: 名前が解決されず、警告も出ない）。監視対象
（キャッシュの files）にも入らないので、そのファイルを直しても再解析され
ない。
"""
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


class MibUppercaseExtensionTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かす。
        # リポジトリ直下の custom_mibs.json に引きずられないようにするため。
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-case-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(self.exe_dir, "mibs"))
        self.cache_file = os.path.join(self.exe_dir, "mib_cache.json")

    def _write_mib(self, name, body):
        path = os.path.join(self.exe_dir, "mibs", name)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(SMI_HEAD + body + SMI_TAIL)
        return path

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_an_uppercase_extension_mib_is_parsed(self):
        """CASE.MIB / CASE.MY / CASE.TXT も解析対象にすること。"""
        self._write_mib("CASE.MIB",
                        "upperMib OBJECT IDENTIFIER ::= { enterprises 5551 }\n")
        self._write_mib("CASE.MY",
                        "upperMy OBJECT IDENTIFIER ::= { enterprises 5552 }\n")
        self._write_mib("CASE.TXT",
                        "upperTxt OBJECT IDENTIFIER ::= { enterprises 5553 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("upperMib"),
                         "1.3.6.1.4.1.5551", "大文字 .MIB が読まれていない")
        self.assertEqual(resolver.resolve_name("upperMy"),
                         "1.3.6.1.4.1.5552", "大文字 .MY が読まれていない")
        self.assertEqual(resolver.resolve_name("upperTxt"),
                         "1.3.6.1.4.1.5553", "大文字 .TXT が読まれていない")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.5551"), "upperMib")

    def test_an_uppercase_extension_mib_is_watched_for_changes(self):
        """大文字拡張子のファイルを直したら再解析されること。"""
        path = self._write_mib(
            "CASE.MIB",
            "upperMib OBJECT IDENTIFIER ::= { enterprises 5551 }\n")
        self._resolver()
        with io.open(self.cache_file, "r", encoding="utf-8") as f:
            self.assertIn("CASE.MIB", json.load(f).get("files", {}),
                          "大文字拡張子のファイルが監視対象に入っていない")

        self._write_mib(
            "CASE.MIB",
            "upperMib OBJECT IDENTIFIER ::= { enterprises 5559 }\n")
        stat = os.stat(path)
        os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("upperMib"),
                         "1.3.6.1.4.1.5559",
                         "変更が再解析されていない")

    def test_a_lowercase_extension_mib_still_works(self):
        """これまでどおり小文字拡張子も読むこと。"""
        self._write_mib("lower.my",
                        "lowerMy OBJECT IDENTIFIER ::= { enterprises 5554 }\n")
        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("lowerMy"), "1.3.6.1.4.1.5554")

    def test_an_unrelated_extension_is_still_ignored(self):
        """MIB でない拡張子は大文字でも読まないこと。"""
        self._write_mib("NOTES.DOC",
                        "notMib OBJECT IDENTIFIER ::= { enterprises 5555 }\n")
        resolver = self._resolver()
        self.assertIsNone(resolver.resolve_name("notMib"))
        with io.open(self.cache_file, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f).get("files", {}), {})


if __name__ == "__main__":
    unittest.main()
