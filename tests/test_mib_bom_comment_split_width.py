r"""末尾コメントに落ちた BOM でも、折り返した見出しでモジュールを区切れることを検証する。

BOM 付きの MIB を `copy /b` で生のまま連結すると、前のファイルに末尾の
改行が無ければ次のファイルの BOM が前のファイルの最後の行に入り込む。
その行が `-- 由来` のようなコメントだと、BOM は「コメントの中」に落ちる。
コメントの中の BOM は、その行の残りがモジュールの見出しちょうどのときだけ
区切り（改行）として扱っている。ところがその判定だけが
`[\w-]+[ \t]+DEFINITIONS[ \t]*::=[ \t]*BEGIN` と 1 行に収まる見出しに
限られていて、コメントの外の判定（`[\w-]+[\s<BOM>]+DEFINITIONS`）や
見出しを探す _MIB_MODULE_HEADER（`\s+`）より狭かった。

実 MIB は見出しを折り返すことがある。折り返した見出しの前に末尾コメントが
来ると区切れず、連結した全部が 1 つのモジュールとして混ざる。

実測（BOM 付き A/B/C を連結。各ファイルは `END` 改行 `-- from vendor X`
で終わり、B・C の見出しが 2 行に折れている）:

    1.3.6.1.4.1.1111 -> aRoot
    1.3.6.1.4.1.2222 -> shared
    1.3.6.1.4.1.3333 -> shared
    1.3.6.1.4.1.3333.1 -> alarmB      ← B-MIB の名前が C-MIB の配下に付いた
    1.3.6.1.4.1.3333.2 -> alarmC
    resolve_oid(1.3.6.1.4.1.3333.1) = alarmB
    resolve_oid(1.3.6.1.4.1.2222.1) = shared.1   ← B の本当の Trap は名前無し

見出しを 1 行へ戻すと A-MIB/B-MIB/C-MIB の 3 区間へ正しく割れるので、
折り返しのときだけ静かに壊れる。同じ周の前のコミットで直した「折り返した
見出し」の修正が、BOM が末尾コメントへ落ちる連結では効いていなかった。

直し方は 2 つ。
(a) コメントの中の判定を、コメントの外と同じ広さにする。名前と
    DEFINITIONS の間・DEFINITIONS と `::= BEGIN` の間に改行を許す。
    行末ちょうどの縛りは残すので、`::= { bogus 9 }` が続く幽霊の見出しは
    これまでどおり弾ける。
(b) 広げるぶん、コメントの中の BOM は「直前のモジュールが END で
    閉じている」ときだけ区切りとして扱う。連結の境目は必ず直前の
    ファイルの END の後ろに来るのに対し、由来を書いたコメントは
    モジュールの途中にあるので、この 1 点で分けられる。(b) が無いと、
    `-- moved from <BOM>OLD-MIB DEFINITIONS ::= BEGIN` のような由来の
    コメントでそこから区間が割れ、後ろの定義が丸ごと落ちる
    （実測: alarmA が resolve_name で None になった）。
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

# 見出しの書き方。実 MIB は名前や `::=` の前で行を折ることがある
HEADER_SHAPES = {
    # 1 行（これまで通る形）
    "one_line": "%s-MIB DEFINITIONS ::= BEGIN\r\n",
    # 名前の直後で折る
    "wrap_before_definitions": "%s-MIB\r\nDEFINITIONS ::= BEGIN\r\n",
    # DEFINITIONS の直後で折る
    "wrap_before_assign": "%s-MIB DEFINITIONS\r\n    ::= BEGIN\r\n",
}

# 由来を書いたコメントに BOM が落ちた MIB。連結ではなく 1 モジュールの
# 途中なので、ここで区間を割ってはいけない
DERIVATION_COMMENT = (
    "A-MIB DEFINITIONS ::= BEGIN\r\n"
    "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
    "shared OBJECT IDENTIFIER ::= { aRoot 1 }\r\n"
    "alarmA NOTIFICATION-TYPE\r\n"
    "    STATUS current\r\n"
    "    -- moved from " + BOM + "OLD-MIB DEFINITIONS ::= BEGIN\r\n"
    "    ::= { shared 1 }\r\n"
    "END\r\n"
    "B-MIB DEFINITIONS ::= BEGIN\r\n"
    "bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }\r\n"
    "shared OBJECT IDENTIFIER ::= { bRoot 1 }\r\n"
    "alarmB NOTIFICATION-TYPE\r\n"
    "    STATUS current\r\n"
    "    ::= { shared 2 }\r\n"
    "END\r\n")


def _module(name, root_index, trap_index, shape, tail):
    """`<name>-MIB` 1 モジュール分の本文

    どのモジュールも `shared` を自分の根の下に置く。区間が割れて
    いなければ後勝ちで 1 つになるので、名前空間が混ざったことが
    Trap の OID に出る。
    """
    lower = name.lower()
    return (HEADER_SHAPES[shape] % name
            + "%sRoot OBJECT IDENTIFIER ::= { enterprises %d }\r\n"
              "shared OBJECT IDENTIFIER ::= { %sRoot 1 }\r\n"
              "alarm%s NOTIFICATION-TYPE\r\n"
              "    STATUS current\r\n"
              "    ::= { shared %d }\r\n"
              "END\r\n" % (lower, root_index, lower, name, trap_index)
            + tail)


class MibBomCommentSplitWidthTest(unittest.TestCase):
    def _resolver(self, data):
        """exe の隣の mibs/ へ ABC.my を置いて読み込む

        場合ごとに新しい一時フォルダを使うので、前の mib_cache.json に
        引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-bom-split-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(exe_dir, "mibs"))
        path = os.path.join(exe_dir, "mibs", "ABC.my")
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        return MIBResolver(), path

    def _concatenated(self, shape):
        """末尾コメントで終わる BOM 付き 3 ファイルを、生のまま連結する"""
        parts = [_module("A", 1111, 1, "one_line", "-- from vendor A"),
                 _module("B", 2222, 2, shape, "-- from vendor B"),
                 _module("C", 3333, 3, shape, "")]
        return b"".join(BOM_BYTES + p.encode("utf-8") for p in parts)

    def test_every_header_shape_splits_after_a_trailing_comment(self):
        """末尾コメントの直後でも、どの見出しの形でも区切れること。"""
        for shape in HEADER_SHAPES:
            with self.subTest(shape=shape):
                resolver, path = self._resolver(self._concatenated(shape))
                modules = {module for _, _, _, module
                           in resolver._extract_mib_definitions(path)}
                self.assertEqual(modules, {"A-MIB", "B-MIB", "C-MIB"},
                                 "区間が割れていない: %r" % sorted(modules))
                self.assertEqual(resolver.resolve_name("alarmA"),
                                 "1.3.6.1.4.1.1111.1.1")
                self.assertEqual(resolver.resolve_name("alarmB"),
                                 "1.3.6.1.4.1.2222.1.2",
                                 "B の Trap が B の shared に付いていない")
                self.assertEqual(resolver.resolve_name("alarmC"),
                                 "1.3.6.1.4.1.3333.1.3")

    def test_no_trap_is_shown_under_another_modules_name(self):
        """よその名前が付いた OID が無いこと（画面に出るのはこちら）。"""
        resolver, _ = self._resolver(
            self._concatenated("wrap_before_definitions"))
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.2222.1.2"),
                         "alarmB")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.3333.1.3"),
                         "alarmC")
        self.assertNotEqual(resolver.resolve_oid("1.3.6.1.4.1.3333.1"),
                            "alarmB",
                            "B の Trap が C の配下に付いている")

    def test_a_derivation_comment_mid_module_does_not_split(self):
        """モジュール途中の由来コメントでは区間を割らないこと。"""
        resolver, path = self._resolver(DERIVATION_COMMENT)
        self.assertEqual(resolver.resolve_name("alarmA"),
                         "1.3.6.1.4.1.1111.1.1",
                         "コメントの中の見出しで alarmA が落ちている")
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.2")
        modules = {module for _, _, _, module
                   in resolver._extract_mib_definitions(path)}
        self.assertEqual(modules, {"A-MIB", "B-MIB"},
                         "幽霊のモジュールができている: %r" % sorted(modules))

    def test_the_same_mibs_without_the_boms_resolve_the_same_way(self):
        """BOM を抜いた同じ並びと、結果が食い違わないこと（対照）。"""
        data = self._concatenated("wrap_before_definitions")
        resolver, _ = self._resolver(data.replace(BOM_BYTES, b"\r\n"))
        self.assertEqual(resolver.resolve_name("alarmB"),
                         "1.3.6.1.4.1.2222.1.2")
        self.assertEqual(resolver.resolve_name("alarmC"),
                         "1.3.6.1.4.1.3333.1.3")


if __name__ == "__main__":
    unittest.main()
