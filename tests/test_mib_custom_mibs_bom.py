"""BOM 付きの custom_mibs.json でも登録した OID 名が消えないことを検証する。

何が起きていたか（基準 aa38a2b で実測）。custom_mibs.json は
_load_custom_mibs() の docstring 自身が「利用者が手で書くファイル」と
書いているとおり、利用者が Windows のエディタで編集するファイルである。
ところが読み込みは encoding='utf-8' のままだったので、メモ帳の
「UTF-8 (BOM 付き)」や PowerShell 5.1 の `Out-File -Encoding utf8` が
付ける BOM があると json.load が

    Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)

で落ちる。受けているのは except Exception でコンソールへ print する
だけなので、画面には何も出ない。中身は正しいのに、利用者が登録した
OID 名だけが全部消えた状態で SNMP の画面が動き続ける。

実測（{"mibs": {"1.3.6.1.4.1.9.2.1.58": "cpuUsage"}} を凍結ビルドの
隣に置いて MIBResolver を作った結果）:

    BOM 無し -> oid_to_name['1.3.6.1.4.1.9.2.1.58'] == 'cpuUsage'
    BOM 付き -> None（ログに上のエラーだけ）

config.json は 9a66c8b で utf-8-sig へ直っているが、そのコミットの
「config.json was the only file left without it」は誤りで、利用者が
手で書くファイルはもう 1 つ残っていた。

どう直したか。custom_mibs.json も utf-8-sig で読む（BOM が無ければ
utf-8 と同じ）。mib_cache.json はアプリが書くファイルで、読めなくても
キャッシュを作り直すだけなので触っていない。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

OID = "1.3.6.1.4.1.9.2.1.58"
NAME = "cpuUsage"


class MibCustomMibsBomTest(unittest.TestCase):
    def setUp(self):
        # exe の隣の custom_mibs.json を読む凍結ビルドとして動かす
        # （リポジトリ直下のファイルに引きずられないため）
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-bom-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        # 空の mibs/ を隣に置く。無いとリポジトリ側の mibs/ を読み、
        # そこへ mib_cache.json を書いてしまう
        os.makedirs(os.path.join(self.exe_dir, "mibs"))

    def _write_custom(self, encoding):
        with io.open(os.path.join(self.exe_dir, "custom_mibs.json"), "w",
                     encoding=encoding) as f:
            json.dump({"mibs": {OID: NAME}}, f)

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_a_bom_does_not_drop_the_custom_names(self):
        """BOM 付きでも、登録した OID 名が引けること。"""
        self._write_custom("utf-8-sig")

        resolver = self._resolver()

        self.assertEqual(resolver.oid_to_name.get(OID), NAME,
                         "BOM だけで利用者の登録が全部消えている")

    def test_a_bom_does_not_drop_the_reverse_lookup(self):
        """BOM 付きでも、名前から OID を引けること。"""
        self._write_custom("utf-8-sig")

        resolver = self._resolver()

        self.assertEqual(resolver.resolve_name(NAME), OID,
                         "BOM だけで逆引きが消えている")

    def test_a_file_without_a_bom_is_still_read(self):
        """BOM 無しは今までどおり読めること（対照）。"""
        self._write_custom("utf-8")

        resolver = self._resolver()

        self.assertEqual(resolver.oid_to_name.get(OID), NAME)

    def test_broken_json_is_still_ignored(self):
        """壊れた JSON は今までどおり無視し、標準 MIB を壊さないこと（対照）。"""
        with io.open(os.path.join(self.exe_dir, "custom_mibs.json"), "w",
                     encoding="utf-8") as f:
            f.write("{ this is not json")

        resolver = self._resolver()

        self.assertNotIn(OID, resolver.oid_to_name)
        self.assertEqual(resolver.resolve_name("sysName"),
                         "1.3.6.1.2.1.1.5.0",
                         "標準 MIB まで巻き添えになっている")


if __name__ == "__main__":
    unittest.main()
