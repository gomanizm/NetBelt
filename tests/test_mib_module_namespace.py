"""同じ名前が別のモジュールで定義されていても、混ざらないことを検証する。

名前→OID の表はモジュールを区別しない 1 枚だったので、2 つの MIB が
同じ名前（例: 両方に `shared`）を自分の根の下で定義すると、後に書いた
方だけが残り、もう一方のモジュールの子がよその親に付く。実測では
sharedTrapA と sharedTrapB が互いの enterprise の下に付いた（入れ替わり）。
Trap が別ベンダーの名前にデコードされることになる。

各ファイルの `MODULE DEFINITIONS ::= BEGIN` からモジュール名を取り、
親は同じモジュールの定義を優先して解決する。同じモジュールに親の定義が
あるのに未解決なら、よその同名を使わずに待つ。同じモジュールに無い
親（IMPORTS）はこれまでどおりモジュールをまたいで探す。

残る制限: 2 つのモジュールが同じ名前を定義し、第三のモジュールがその
一方を IMPORTS しているとき、どちらを指すかは IMPORTS を見ていないので
決められず、後に解決した方になる。
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


class MibModuleNamespaceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-mib-ns-")
        self.mibs = os.path.join(self.dir, "mibs")
        os.makedirs(self.mibs)
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._cwd)

    def _write(self, name, module, body):
        with io.open(os.path.join(self.mibs, name), "w", encoding="utf-8") as f:
            f.write("%s DEFINITIONS ::= BEGIN\n%s\nEND\n" % (module, body))

    def _resolved(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()._load_or_update_mib_cache("mibs")

    def _write_two_modules_sharing_a_name(self, b_root_is_module_identity=False):
        """A-MIB と B-MIB が同じ名前 `shared` を自分の根の下に定義する。

        b_root_is_module_identity を立てると B の根を MODULE-IDENTITY に
        する。抽出は型ごとにまとめて行われるので、B の `shared` は根より
        先に並び、最初の回では解決できない。その回に B の Trap が A の
        `shared` に付く、というのが実測された経路。
        """
        self._write("A.my", "A-MIB",
                    "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\n"
                    "shared OBJECT IDENTIFIER ::= { aRoot 1 }\n"
                    "sharedTrapA NOTIFICATION-TYPE\n"
                    "    STATUS current\n"
                    "    ::= { shared 1 }\n")
        if b_root_is_module_identity:
            b_root = ("bRoot MODULE-IDENTITY\n"
                      "    LAST-UPDATED \"202601010000Z\"\n"
                      "    ::= { enterprises 2222 }\n")
        else:
            b_root = "bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }\n"
        self._write("B.my", "B-MIB",
                    b_root
                    + "shared OBJECT IDENTIFIER ::= { bRoot 1 }\n"
                    "sharedTrapB NOTIFICATION-TYPE\n"
                    "    STATUS current\n"
                    "    ::= { shared 1 }\n")

    def test_children_hang_under_their_own_modules_parent(self):
        """同名の親があっても、子は自分のモジュールの親に付くこと。"""
        self._write_two_modules_sharing_a_name()
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.1111.1.1"), "sharedTrapA",
                         "A の Trap が A の下に無い: %s" % resolved)
        self.assertEqual(resolved.get("1.3.6.1.4.1.2222.1.1"), "sharedTrapB",
                         "B の Trap が B の下に無い: %s" % resolved)

    def test_both_same_named_nodes_keep_their_own_oid(self):
        """`shared` が両方の OID から引けること（後勝ちで消えない）。"""
        self._write_two_modules_sharing_a_name()
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.1111.1"), "shared")
        self.assertEqual(resolved.get("1.3.6.1.4.1.2222.1"), "shared")

    def test_the_same_module_parent_wins_even_if_it_resolves_later(self):
        """自分のモジュールの親が後の回で解決しても、よその親を使わないこと。"""
        self._write_two_modules_sharing_a_name(b_root_is_module_identity=True)
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.2222.1.1"), "sharedTrapB",
                         "B の Trap が A の shared に付いた: %s" % resolved)
        self.assertEqual(resolved.get("1.3.6.1.4.1.1111.1.1"), "sharedTrapA",
                         "A の Trap が B の Trap に上書きされた: %s" % resolved)

    def test_an_imported_parent_from_another_module_still_resolves(self):
        """親が別モジュールにしか無ければ、これまでどおり越えて解決すること。"""
        self._write("SMI.my", "VENDOR-SMI",
                    "vendorRoot OBJECT IDENTIFIER ::= { enterprises 3333 }\n"
                    "vendorMgmt OBJECT IDENTIFIER ::= { vendorRoot 9 }\n")
        self._write("USE.my", "VENDOR-USE-MIB",
                    "IMPORTS vendorMgmt FROM VENDOR-SMI;\n"
                    "useMIB MODULE-IDENTITY\n"
                    "    LAST-UPDATED \"202601010000Z\"\n"
                    "    ::= { vendorMgmt 4 }\n"
                    "useTrap NOTIFICATION-TYPE\n"
                    "    STATUS current\n"
                    "    ::= { useMIB 1 }\n")
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.3333.9.4"), "useMIB",
                         "別モジュールの親を越えて解決できない: %s" % resolved)
        self.assertEqual(resolved.get("1.3.6.1.4.1.3333.9.4.1"), "useTrap")

    def test_a_file_without_a_module_header_is_still_read(self):
        """`DEFINITIONS ::= BEGIN` が無いファイルでも落ちないこと。"""
        with io.open(os.path.join(self.mibs, "bare.my"), "w",
                     encoding="utf-8") as f:
            f.write("bareRoot OBJECT IDENTIFIER ::= { enterprises 4444 }\n"
                    "bareLeaf OBJECT IDENTIFIER ::= { bareRoot 1 }\n")
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.4444.1"), "bareLeaf")


if __name__ == "__main__":
    unittest.main()
