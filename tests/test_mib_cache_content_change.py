"""MIB ファイルの中身を差し替えたら、mtime が同じでも作り直すことを検証する。

mib_cache.json の files はファイル名と mtime だけを記録していたので、
中身を差し替えても mtime が保たれていれば、再起動後も古い OID 対応を
使い続けた。実測: V.my（linkFlapOld={vRoot 1}）でキャッシュを作ったあと、
中身を fanFailure={vRoot 1}・psuFailure={vRoot 2} に書き換えて os.utime で
元の mtime に戻すと、3333.1 -> linkFlapOld、3333.2 -> vRoot.2 のまま。
利用者の操作でも起きる: 同じ entry 日時を持つ v1 と v2 の ZIP を
Expand-Archive -Force で順に展開すると mtime が同じになり、同じく古い
linkFlapOld のままだった。custom_mibs.json の中身の指紋はこの経路には
効かない。

直し方: files にファイルごとの mtime・大きさ・中身の sha1 を記録し、
どれかが違えば作り直す。費用は中身を読む分で、読み込みはバックグラウンドの
スレッドで動く。記録の形が変わるので MIB_PARSER_VERSION を上げた。
中身を読めない（排他ロックなど）ファイルは、mtime と大きさが記録どおり
なら前回の解析結果を使う（これまでどおり）。
"""
import builtins
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

SMI_HEAD = "TEST-SMI DEFINITIONS ::= BEGIN\n"
SMI_TAIL = "\nEND\n"
ROOT = "vRoot OBJECT IDENTIFIER ::= { enterprises 3333 }\n"


class MibCacheContentChangeTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ と mib_cache.json を置く凍結ビルドとして動かし、
        # MIBResolver() を丸ごと作る（起動時に MIB を読む経路）。
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-content-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(self.exe_dir, "mibs"))
        self.path = os.path.join(self.exe_dir, "mibs", "V.my")

    def _write_mib(self, body):
        with io.open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(SMI_HEAD + ROOT + body + SMI_TAIL)

    def _replace_keeping_mtime(self, body):
        """中身を書き換え、mtime を元に戻す（固定日時の配布物の展開と同じ）。"""
        stat = os.stat(self.path)
        self._write_mib(body)
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(os.stat(self.path).st_mtime_ns, stat.st_mtime_ns)

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_a_replaced_file_with_the_same_mtime_is_parsed_again(self):
        """mtime が同じでも、中身が変われば新しい名前が引けること。"""
        self._write_mib("linkFlapOld OBJECT IDENTIFIER ::= { vRoot 1 }\n")
        self.assertEqual(self._resolver().resolve_oid("1.3.6.1.4.1.3333.1"),
                         "linkFlapOld")

        self._replace_keeping_mtime(
            "fanFailure OBJECT IDENTIFIER ::= { vRoot 1 }\n"
            "psuFailure OBJECT IDENTIFIER ::= { vRoot 2 }\n")
        resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.3333.1"),
                         "fanFailure", "差し替え前の名前が使われている")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.3333.2"),
                         "psuFailure")

    def test_a_same_size_edit_with_the_same_mtime_is_parsed_again(self):
        """大きさも mtime も同じ書き換え（名前の付け替え）も拾うこと。"""
        self._write_mib("linkFlapOld OBJECT IDENTIFIER ::= { vRoot 1 }\n")
        self._resolver()
        size = os.path.getsize(self.path)

        self._replace_keeping_mtime(
            "linkFlapNew OBJECT IDENTIFIER ::= { vRoot 1 }\n")
        self.assertEqual(os.path.getsize(self.path), size)
        self.assertEqual(self._resolver().resolve_oid("1.3.6.1.4.1.3333.1"),
                         "linkFlapNew", "差し替え前の名前が使われている")

    def test_a_file_locked_at_start_still_uses_the_previous_parse(self):
        """記録どおりのファイルが起動時に読めなくても、前回の結果を使うこと。"""
        self._write_mib("linkFlapOld OBJECT IDENTIFIER ::= { vRoot 1 }\n")
        self._resolver()

        real_open = builtins.open

        def locked_open(file, *args, **kwargs):
            if isinstance(file, str) and os.path.basename(file) == "V.my":
                raise PermissionError(13, "Permission denied", file)
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=locked_open):
            resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.3333.1"),
                         "linkFlapOld",
                         "読めないだけの変わっていないファイルの定義が消えた")


if __name__ == "__main__":
    unittest.main()
