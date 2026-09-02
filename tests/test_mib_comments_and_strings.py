"""コメントと文字列の中身が、MIB の定義として読まれないことを検証する。

定義の抽出は、型キーワードから次の `::= { 親 添字 }` までを最短一致で
拾う。生のテキストに対して掛けるので、その間の DESCRIPTION 文字列や
`--` コメントの中に `::= { x n }` と書いてあると、そこで止まって
偽の親を記録する（親が未定義なら黙って落ちる）。

より現実的なのはコメントアウトされた定義で、ベンダー MIB では廃止した
オブジェクトを `-- oldName OBJECT-TYPE` のまま残すことが普通にある。
その行が一致の起点になって次の本物の `::=` を飲み込み、本物の名前が
消えたうえに別の OID に付く。

コメントと文字列の中身を先に消してから拾う。
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

SMI_HEAD = "TEST-SMI DEFINITIONS ::= BEGIN\n"
SMI_TAIL = "\nEND\n"


class MibCommentsAndStringsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-mib-cs-")
        self.mibs = os.path.join(self.dir, "mibs")
        os.makedirs(self.mibs)
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._cwd)

    def _write(self, name, body):
        with io.open(os.path.join(self.mibs, name), "w", encoding="utf-8") as f:
            f.write(SMI_HEAD + body + SMI_TAIL)

    def _resolved(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()._load_or_update_mib_cache("mibs")

    def test_an_assignment_quoted_in_a_description_is_ignored(self):
        """DESCRIPTION の中の `::= { bogus 99 }` に釣られないこと。"""
        self._write("A.my",
                    "realParent OBJECT IDENTIFIER ::= { enterprises 11111 }\n"
                    "bogus      OBJECT IDENTIFIER ::= { enterprises 22222 }\n"
                    "myObj OBJECT-TYPE\n"
                    "    SYNTAX INTEGER\n"
                    '    DESCRIPTION "Written as e.g. ::= { bogus 99 } in older drafts"\n'
                    "    ::= { realParent 5 }\n")
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.11111.5"), "myObj",
                         "本物の親に付いていない: %s" % resolved)
        self.assertNotIn("1.3.6.1.4.1.22222.99", resolved,
                         "DESCRIPTION の中身を定義として読んでいる")

    def test_an_assignment_in_a_comment_is_ignored(self):
        """`--` コメントの中の `::= { x 1 }` に釣られないこと。"""
        self._write("B.my",
                    "realParent OBJECT IDENTIFIER ::= { enterprises 11111 }\n"
                    "x          OBJECT IDENTIFIER ::= { enterprises 33333 }\n"
                    "myObj OBJECT-TYPE\n"
                    "    SYNTAX INTEGER\n"
                    "    -- was ::= { x 1 } before 2.0\n"
                    "    ::= { realParent 7 }\n")
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.11111.7"), "myObj")
        self.assertNotIn("1.3.6.1.4.1.33333.1", resolved,
                         "コメントの中身を定義として読んでいる")

    def test_a_commented_out_definition_does_not_swallow_the_next_one(self):
        """コメントアウトされた `-- old OBJECT-TYPE` が次の定義を飲まないこと。"""
        self._write("C.my",
                    "realParent OBJECT IDENTIFIER ::= { enterprises 11111 }\n"
                    "-- oldObj OBJECT-TYPE\n"
                    "--     SYNTAX INTEGER\n"
                    "--     ::= { realParent 1 }\n"
                    "newObj OBJECT-TYPE\n"
                    "    SYNTAX INTEGER\n"
                    '    DESCRIPTION "current"\n'
                    "    ::= { realParent 2 }\n")
        resolved = self._resolved()
        self.assertEqual(resolved.get("1.3.6.1.4.1.11111.2"), "newObj",
                         "本物の定義が消えている: %s" % resolved)
        self.assertNotIn("oldObj", set(resolved.values()),
                         "コメントアウトされた定義を拾っている")

    def test_a_colon_in_a_description_still_resolves(self):
        """前の修正（本文のコロン）を壊していないこと。"""
        self._write("D.my",
                    "realParent OBJECT IDENTIFIER ::= { enterprises 11111 }\n"
                    "myTrap NOTIFICATION-TYPE\n"
                    "    STATUS current\n"
                    '    DESCRIPTION "Reference: RFC 1234, see http://example.com/x"\n'
                    "    ::= { realParent 9 }\n")
        self.assertEqual(self._resolved().get("1.3.6.1.4.1.11111.9"), "myTrap")

    def test_a_double_dash_inside_a_string_is_not_a_comment(self):
        """文字列の中の `--` をコメントの始まりと見ないこと。"""
        self._write("E.my",
                    "realParent OBJECT IDENTIFIER ::= { enterprises 11111 }\n"
                    "myObj OBJECT-TYPE\n"
                    "    SYNTAX INTEGER\n"
                    '    DESCRIPTION "range -- inclusive -- of ports"\n'
                    "    ::= { realParent 3 }\n")
        self.assertEqual(self._resolved().get("1.3.6.1.4.1.11111.3"), "myObj")


if __name__ == "__main__":
    unittest.main()
