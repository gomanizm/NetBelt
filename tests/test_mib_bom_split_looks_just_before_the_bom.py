r"""BOM でモジュールを区切るかを、その BOM の直前だけで決めることを検証する。

コメントの中の BOM（U+FEFF）は「行の残りがモジュールの見出しちょうど」で、
かつ「直前のモジュールが END で閉じている」ときだけ連結の区切りとして
扱う。その「閉じているか」を、直前の BOM の次からこの BOM までに END が
あるか、という区間の走査で見ていた。区間の取り方のせいで 2 方向に外れる。

実測 1（取りこぼし）: 1 ファイルに A-MIB と B-MIB を並べ、2 番目の
B-MIB の中に由来コメント `-- moved from <BOM>OLD-MIB DEFINITIONS ::=
BEGIN` を 1 行置く。走査の区間に A-MIB の END が入るので、モジュールの
途中なのに区切ってしまい、alarmB が丸ごと落ちる（resolve_name('alarmB')
が None）。由来コメントを 1 番目のモジュールに置いた形だけが通っていた。

実測 2（回帰）: 連結の区切りの BOM の直前にもう 1 つ BOM があると
（空の BOM だけのファイルを挟んで連結した形）、走査の区間が空になって
END が見つからず区切れない。modules が ['A-MIB'] だけになり、A の Trap が
B の enterprise の下（1.3.6.1.4.1.2222.1.1）に付く。

直し方: 「直前の BOM からこの位置までに END があるか」ではなく「この BOM の
直前の実テキストが END で終わっているか」を見る。コメントと文字列は
空白になっているので、由来コメントの直前は `STATUS current` などで終わって
END にならず、連結の境目だけが END で終わる、という不変条件をそのまま
式にする。後ろ向きに空白だけを飛ばすので、BOM の数やファイルの長さに
関係なく一定時間で決まる。抽出の規則が変わるので MIB_PARSER_VERSION を
上げた。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# ソースへ直に書くと目に見えないので番号から作る
BOM = chr(0xFEFF)
BOM_BYTES = BOM.encode("utf-8")
NL = "\r\n"


def _module(name, root_index, trap_index, derivation=False, tail=""):
    """`<name>-MIB` 1 モジュール分の本文

    どのモジュールも `shared` を自分の根の下に置く。区間が割れて
    いなければ後勝ちで 1 つになるので、名前空間が混ざったことが
    Trap の OID に出る。derivation を立てると、Trap の本体へ BOM 入りの
    由来コメントを 1 行入れる（連結の区切りではなくモジュールの途中）。
    """
    lower = name.lower()
    body = (name + "-MIB DEFINITIONS ::= BEGIN" + NL
            + lower + "Root OBJECT IDENTIFIER ::= { enterprises %d }"
            % root_index + NL
            + "shared OBJECT IDENTIFIER ::= { " + lower + "Root 1 }" + NL
            + "alarm" + name + " NOTIFICATION-TYPE" + NL
            + "    STATUS current" + NL)
    if derivation:
        body += ("    -- moved from " + BOM + "OLD-MIB DEFINITIONS ::= BEGIN"
                 + NL)
    body += ("    ::= { shared %d }" % trap_index + NL
             + "END" + NL + tail)
    return body


class MibBomSplitLooksJustBeforeTheBomTest(unittest.TestCase):
    def _resolver(self, data):
        """exe の隣の mibs/ へ AB.my を置いて読み込む

        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-bom-before-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(exe_dir, "mibs"))
        path = os.path.join(exe_dir, "mibs", "AB.my")
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        return MIBResolver(), path

    def test_a_derivation_comment_in_a_later_module_does_not_split(self):
        """2 番目のモジュールの由来コメントで区間を割らないこと。"""
        data = (BOM_BYTES
                + (_module("A", 1111, 1)
                   + _module("B", 2222, 2, derivation=True)).encode("utf-8"))
        resolver, path = self._resolver(data)
        modules = sorted({module for _, _, _, module
                          in resolver._extract_mib_definitions(path)})
        self.assertEqual(modules, ["A-MIB", "B-MIB"],
                         "幽霊のモジュールができている: %r" % (modules,))
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.2",
                         "由来コメントで alarmB が落ちている")
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1.1")

    def test_the_same_file_without_the_bom_resolves_the_same_way(self):
        """BOM を抜いた同じ MIB と、結果が食い違わないこと（対照）。"""
        data = (_module("A", 1111, 1)
                + _module("B", 2222, 2, derivation=True)).replace(BOM, "")
        resolver, _ = self._resolver(data)
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.2")

    def test_two_boms_in_a_row_still_start_a_new_module(self):
        """連結の区切りに BOM が 2 つ続いても、モジュールを区切れること。"""
        head = _module("A", 1111, 1, tail="-- from vendor A")
        tail = _module("B", 2222, 2)
        for label, seam in (("single", BOM_BYTES), ("double", BOM_BYTES * 2)):
            with self.subTest(seam=label):
                data = (BOM_BYTES + head.encode("utf-8")
                        + seam + tail.encode("utf-8"))
                resolver, path = self._resolver(data)
                modules = sorted({module for _, _, _, module
                                  in resolver._extract_mib_definitions(path)})
                self.assertEqual(modules, ["A-MIB", "B-MIB"],
                                 "区間が割れていない: %r" % (modules,))
                self.assertEqual(resolver.resolve_name("alarmA"),
                                 "1.3.6.1.4.1.1111.1.1",
                                 "A の Trap が B の enterprise に付いている")
                self.assertEqual(resolver.resolve_name("alarmB"),
                                 "1.3.6.1.4.1.2222.1.2")

    def test_no_trap_is_shown_under_another_vendors_oid(self):
        """よその企業 OID に別モジュールの名前が付かないこと。"""
        data = (BOM_BYTES
                + _module("A", 1111, 1, tail="-- from vendor A")
                .encode("utf-8")
                + BOM_BYTES * 2 + _module("B", 2222, 2).encode("utf-8"))
        resolver, _ = self._resolver(data)
        self.assertNotEqual(resolver.resolve_oid("1.3.6.1.4.1.2222.1.1"),
                            "alarmA",
                            "B の企業 OID に A の Trap の名前が付いている")


if __name__ == "__main__":
    unittest.main()
