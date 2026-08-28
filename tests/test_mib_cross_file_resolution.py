"""mibs/ に置いた MIB が名前解決されることを検証する。

_parse_mib_file_to_dict は解析の起点となる名前表を
`temp_name_to_oid = dict(self.name_to_oid)` とファイルごとに作り直して
いた。self.name_to_oid が更新されるのは全ファイルを解析し終えた後なので、
あるファイルで定義した名前は次のファイルの解析時に見えない。ベンダー
MIB は親ノードを別ファイル（Cisco なら CISCO-SMI.my の ciscoMgmt）で
定義するのが普通なので、そこにぶら下がる定義は全滅していた。

さらに拾う定義は OBJECT IDENTIFIER / OBJECT-TYPE / NOTIFICATION-TYPE の
3つだけで、MODULE-IDENTITY を見ていなかった。現代の MIB はモジュールの
根を MODULE-IDENTITY で定義するため、1ファイルで完結していても根が
解決できず、その配下（Trap が実際に運ぶ通知 OID を含む）が丸ごと落ちる。

共有の辞書へ変えるだけでは足りない。os.listdir() が返す順に解決すると、
親が後ろのファイルにある定義は依然として落ちる。進まなくなるまで
繰り返す必要がある。

v1.2.0 では著作権上の理由で mibs/ の実ファイルを同梱していないので、
ここは利用者が自分で置いた MIB が読めるかどうかの話になる。
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

SMI_HEAD = "TEST-SMI DEFINITIONS ::= BEGIN\n"
SMI_TAIL = "\nEND\n"


class MibCrossFileResolutionTest(unittest.TestCase):
    def setUp(self):
        # MIBResolver は 'mibs' も 'mib_cache.json' も作業ディレクトリ相対で
        # 読み書きする。リポジトリを汚さないよう、一時ディレクトリへ移る。
        self.dir = tempfile.mkdtemp(prefix="netbelt-mib-")
        self.mibs = os.path.join(self.dir, "mibs")
        os.makedirs(self.mibs)
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._cwd)

    def _write(self, name, body):
        with io.open(os.path.join(self.mibs, name), "w", encoding="utf-8") as f:
            f.write(SMI_HEAD + body + SMI_TAIL)

    def _resolved(self):
        """mibs/ を読み込んだ結果の OID→名前を返す。"""
        from core.mib_resolver import MIBResolver
        resolver = MIBResolver()
        return resolver._load_or_update_mib_cache("mibs")

    def test_a_definition_whose_parent_is_in_another_file_resolves(self):
        """親が別ファイルにあっても解決すること。"""
        self._write("A-SMI.my",
                    "aVendor OBJECT IDENTIFIER ::= { enterprises 99999 }\n"
                    "aMgmt   OBJECT IDENTIFIER ::= { aVendor 2 }\n")
        self._write("B-USE.my",
                    "bRoot OBJECT IDENTIFIER ::= { aMgmt 41 }\n"
                    "bTrap NOTIFICATION-TYPE\n"
                    "    STATUS current\n"
                    "    ::= { bRoot 2 }\n")

        names = set(self._resolved().values())
        self.assertIn("bRoot", names, "別ファイルの親にぶら下がる定義が落ちている")
        self.assertIn("bTrap", names, "Trap が運ぶ通知 OID が落ちている")

    def test_the_resolved_oid_is_correct(self):
        """組み立てた OID が正しいこと。"""
        self._write("A-SMI.my",
                    "aVendor OBJECT IDENTIFIER ::= { enterprises 99999 }\n"
                    "aMgmt   OBJECT IDENTIFIER ::= { aVendor 2 }\n")
        self._write("B-USE.my",
                    "bRoot OBJECT IDENTIFIER ::= { aMgmt 41 }\n")

        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.99999.2.41"), "bRoot",
                         "組み立てた OID が違う: %s" % resolved)

    def test_the_order_the_files_are_read_in_does_not_matter(self):
        """子のファイルが先に読まれても解決すること。

        os.listdir() の順に一度だけ解決すると、親が後ろのファイルに
        ある定義が落ちる。ここではファイル名で子が先に来るようにしてある。
        """
        self._write("aaa-child.my",
                    "cChild OBJECT IDENTIFIER ::= { cParent 7 }\n")
        self._write("zzz-parent.my",
                    "cParent OBJECT IDENTIFIER ::= { enterprises 88888 }\n")

        names = set(self._resolved().values())
        self.assertIn("cChild", names,
                      "読み込み順に依存して落ちている")

    def test_a_module_identity_root_is_picked_up(self):
        """MODULE-IDENTITY で定義された根を拾うこと。"""
        self._write("C-MOD.my",
                    "cVendor OBJECT IDENTIFIER ::= { enterprises 77777 }\n"
                    "cMib MODULE-IDENTITY\n"
                    "    LAST-UPDATED \"202601010000Z\"\n"
                    "    ORGANIZATION \"test\"\n"
                    "    ::= { cVendor 7 }\n")

        names = set(self._resolved().values())
        self.assertIn("cMib", names, "MODULE-IDENTITY の根を拾えていない")

    def test_what_hangs_under_a_module_identity_root_resolves(self):
        """その配下も解決すること（Trap が運ぶ通知 OID がここに来る）。"""
        self._write("C-MOD.my",
                    "cVendor OBJECT IDENTIFIER ::= { enterprises 77777 }\n"
                    "cMib MODULE-IDENTITY\n"
                    "    LAST-UPDATED \"202601010000Z\"\n"
                    "    ::= { cVendor 7 }\n"
                    "cTrap NOTIFICATION-TYPE\n"
                    "    STATUS current\n"
                    "    ::= { cMib 3 }\n")

        resolved = self._resolved()
        self.assertIn("cTrap", set(resolved.values()),
                      "MODULE-IDENTITY の配下が丸ごと落ちている")
        self.assertEqual(resolved.get("1.3.6.1.4.1.77777.7.3"), "cTrap")

    def test_a_description_containing_a_colon_does_not_hide_the_definition(self):
        """DESCRIPTION に `:` があっても定義を拾えること。

        型キーワードから `::=` までを「コロンを含まない並び」として
        探すと、`DESCRIPTION "Reference: RFC 1234"` や URL のように
        本文へコロンが入る定義を全部取りこぼす。実 MIB はほぼ必ず
        DESCRIPTION を持ち、そこに RFC 参照や URL を書くので、
        これに当たると名前解決はほとんど働かない。
        """
        self._write("E-DESC.my",
                    "eVendor OBJECT IDENTIFIER ::= { enterprises 55555 }\n"
                    "eTrap NOTIFICATION-TYPE\n"
                    "    STATUS current\n"
                    '    DESCRIPTION "Reference: RFC 1234, '
                    'see http://example.com/mib"\n'
                    "    ::= { eVendor 9 }\n")

        resolved = self._resolved()
        self.assertIn("eTrap", set(resolved.values()),
                      "本文にコロンがある定義を取りこぼしている")
        self.assertEqual(resolved.get("1.3.6.1.4.1.55555.9"), "eTrap")

    def test_an_object_type_with_a_colon_in_its_description_resolves(self):
        """OBJECT-TYPE でも同じこと。"""
        self._write("F-DESC.my",
                    "fVendor OBJECT IDENTIFIER ::= { enterprises 44444 }\n"
                    "fName OBJECT-TYPE\n"
                    "    SYNTAX DisplayString\n"
                    '    DESCRIPTION "Format: name:value"\n'
                    "    ::= { fVendor 4 }\n")

        self.assertIn("fName", set(self._resolved().values()),
                      "本文にコロンがある OBJECT-TYPE を取りこぼしている")

    def test_a_definition_with_no_parent_anywhere_is_simply_skipped(self):
        """親がどこにも無い定義で止まらないこと。"""
        self._write("D-ORPHAN.my",
                    "dGood OBJECT IDENTIFIER ::= { enterprises 66666 }\n"
                    "dOrphan OBJECT IDENTIFIER ::= { neverDefined 1 }\n")

        names = set(self._resolved().values())
        self.assertIn("dGood", names, "解決できる定義まで巻き添えになっている")
        self.assertNotIn("dOrphan", names)


if __name__ == "__main__":
    unittest.main()
