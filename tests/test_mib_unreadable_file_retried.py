"""一時的に読めなかった MIB ファイルを、次の起動で読み直すことを検証する。

_extract_mib_definitions() は open/read の例外も握りつぶして空の結果を
返し、呼び出し側はそのファイルの mtime もキャッシュの files に記録して
いた。以後はファイルが読めるようになっても mtime が同じなのでキャッシュが
使われ、そのファイルの定義は欠けたままになる。実測（A.my に aTrap、
B.my に bTrap。1 回目だけ B.my の open を PermissionError にする。
他プロセスの排他ロックでも同じ結果）: 1 回目は「B.my: 0件」「キャッシュを
更新しました」で bTrap=None。読める状態に戻して 2 回・3 回と作り直しても
「（キャッシュ使用）」で bTrap=None、1.3.6.1.4.1.2222.1 は
'enterprises.2222.1' のまま。os.utime で mtime を変えるまで直らない。
ウイルス対策・同期ソフトの排他ロックや ACL で読めなかった MIB は、ACL を
直しても mtime が変わらないので、キャッシュを消すかファイルに触れるまで
直らない。欠落を知らせるのは標準出力だけ。

直し方: 読めなかったこと（OSError）は空の結果にせず呼び出し側へ返し、
呼び出し側はそのファイルを files に記録しない。次の起動では files と
mibs/ の中身が食い違うので、必ず解析し直す。
"""
import builtins
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


class MibUnreadableFileRetriedTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かし、
        # MIBResolver() を丸ごと作る（起動時に MIB を読む経路）。
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-unreadable-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(self.exe_dir, "mibs"))
        self.cache_file = os.path.join(self.exe_dir, "mib_cache.json")
        self._write_mib("A.my",
                        "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\n"
                        "aTrap OBJECT IDENTIFIER ::= { aRoot 1 }\n")
        self._write_mib("B.my",
                        "bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }\n"
                        "bTrap OBJECT IDENTIFIER ::= { bRoot 1 }\n")

    def _write_mib(self, name, body):
        with io.open(os.path.join(self.exe_dir, "mibs", name), "w",
                     encoding="utf-8") as f:
            f.write(SMI_HEAD + body + SMI_TAIL)

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def _resolver_while_unreadable(self, name):
        """name の open だけが PermissionError になる状態で作る。"""
        real_open = builtins.open

        def locked_open(file, *args, **kwargs):
            if isinstance(file, str) and os.path.basename(file) == name:
                raise PermissionError(13, "Permission denied", file)
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=locked_open):
            return self._resolver()

    def test_a_file_that_could_not_be_read_is_parsed_on_the_next_start(self):
        """読めなかった MIB が、読めるようになった次の起動で解決されること。"""
        first = self._resolver_while_unreadable("B.my")
        self.assertIsNone(first.resolve_name("bTrap"))
        self.assertEqual(first.resolve_name("aTrap"), "1.3.6.1.4.1.1111.1")

        second = self._resolver()
        self.assertEqual(second.resolve_name("bTrap"), "1.3.6.1.4.1.2222.1",
                         "読めるようになっても欠けたキャッシュが使われている")
        self.assertEqual(second.resolve_oid("1.3.6.1.4.1.2222.1"), "bTrap")
        self.assertEqual(second.resolve_name("aTrap"), "1.3.6.1.4.1.1111.1")

    def test_a_file_that_could_not_be_read_is_not_recorded_as_parsed(self):
        """読めなかったファイルをキャッシュの files に記録しないこと。"""
        self._resolver_while_unreadable("B.my")
        with io.open(self.cache_file, "r", encoding="utf-8") as f:
            files = json.load(f).get("files", {})
        self.assertIn("A.my", files)
        self.assertNotIn("B.my", files,
                         "読めなかったファイルが解析済みとして記録された")

    def test_the_cache_is_reused_once_every_file_was_read(self):
        """全部読めた後は、これまでどおりキャッシュを使うこと。"""
        self._resolver_while_unreadable("B.my")
        self._resolver()
        with io.open(self.cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["mibs"]["9.9.9"] = "servedFromCache"
        with io.open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("9.9.9"), "servedFromCache",
                         "変更が無いのにキャッシュを作り直している")


if __name__ == "__main__":
    unittest.main()
