"""壊れた custom_mibs.json が辞書に残らないことを検証する。

custom_mibs.json は利用者が手で書けるファイルなので、値が文字列で
ないこともある。読み込みは `oid_to_name.update(custom_mibs)` を先に
済ませてから逆引きを組んでいたため、逆引きで例外（名前が list なら
unhashable）になると、

  * 壊れた値は oid_to_name に残ったまま、
  * それより後ろにある正常な名前は逆引きに入らない、

という片側更新になった。残った非文字列は resolve_oid() がそのまま
返すので、受け取った側（Trap 表の QStandardItem）が TypeError で
落ちる。Trap が1件届くたびにエラーダイアログが出て、その OID の
Trap は表に載らない。

読み込み時に各エントリを検証し、不正なものだけ捨てて、辞書は検証後に
まとめて反映する。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class MibCustomEntryValidationTest(unittest.TestCase):
    def setUp(self):
        # exe の隣の custom_mibs.json を読む凍結ビルドとして動かす
        # （リポジトリ直下のファイルに引きずられないため）
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-custom-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        # 空の mibs/ を隣に置く。無いとリポジトリ側の mibs/ を読み、
        # そこへ mib_cache.json を書いてしまう
        os.makedirs(os.path.join(self.exe_dir, "mibs"))

    def _write_custom(self, mibs):
        with io.open(os.path.join(self.exe_dir, "custom_mibs.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"mibs": mibs}, f)

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_a_non_string_name_does_not_stay_in_the_forward_table(self):
        """名前が文字列でないエントリは登録しないこと。"""
        self._write_custom({"1.3.6.1.4.1.99999.2": ["broken"],
                            "1.3.6.1.4.1.99999.1": "okName"})

        resolver = self._resolver()
        self.assertNotIn("1.3.6.1.4.1.99999.2", resolver.oid_to_name,
                         "壊れた値が辞書に残っている")

    def test_resolve_oid_always_returns_a_string(self):
        """resolve_oid() が文字列以外を返さないこと。"""
        self._write_custom({"1.3.6.1.4.1.99999.2": ["broken"],
                            "1.3.6.1.4.1.99999.1": "okName"})

        resolver = self._resolver()
        self.assertIsInstance(resolver.resolve_oid("1.3.6.1.4.1.99999.2"), str)
        for name in resolver.oid_to_name.values():
            self.assertIsInstance(name, str, "辞書に非文字列の名前がある")

    def test_a_valid_entry_after_a_broken_one_is_still_registered(self):
        """壊れたエントリの後ろにある正常な名前も登録されること。"""
        self._write_custom({"1.3.6.1.4.1.99999.2": ["broken"],
                            "1.3.6.1.4.1.99999.1": "okName"})

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("okName"),
                         "1.3.6.1.4.1.99999.1",
                         "壊れたエントリの巻き添えで逆引きが欠けている")
        self.assertEqual(resolver.oid_to_name.get("1.3.6.1.4.1.99999.1"),
                         "okName")

    def test_an_oid_that_is_not_a_dotted_number_is_dropped(self):
        """OID の形をしていないキーは捨てること。"""
        self._write_custom({"not an oid": "badOid",
                            "1.3.6.1.4.1.99999.3": "goodOid"})

        resolver = self._resolver()
        self.assertIsNone(resolver.resolve_name("badOid"),
                          "OID でないキーが登録されている")
        self.assertNotIn("not an oid", resolver.oid_to_name)
        self.assertEqual(resolver.resolve_name("goodOid"),
                         "1.3.6.1.4.1.99999.3")

    def test_a_mibs_value_that_is_not_a_mapping_is_ignored(self):
        """mibs が辞書でなくても、標準 MIB の辞書を壊さないこと。"""
        self._write_custom(["1.3.6.1.4.1.99999.4", "listInsteadOfDict"])

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_name("sysName"),
                         "1.3.6.1.2.1.1.5.0",
                         "標準 MIB まで巻き添えになっている")

    def test_normal_entries_are_loaded_as_before(self):
        """正常な custom_mibs.json はこれまでどおり読めること。"""
        self._write_custom({"1.3.6.1.4.1.99999.1": "myRoot",
                            "1.3.6.1.4.1.99999.1.2": "myChild"})

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.99999.1"), "myRoot")
        self.assertEqual(resolver.resolve_name("myChild"),
                         "1.3.6.1.4.1.99999.1.2")


if __name__ == "__main__":
    unittest.main()
