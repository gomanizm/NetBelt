r"""親の解決を諦めるのが、本当に曖昧なときだけであることを検証する。

利用者の決定（2026-09-20）: どのモジュールの親か確定できないときは
名前を付けず OID のまま出す（誤った他社の名前を出すより安全）。判断に
使った条件と、諦めた件数を標準出力に 1 行残す。

前の周の実装は、決定の範囲を超えて諦めていた。条件が「そのモジュールが
その名前を宣言していて、自分のモジュールでは OID が決まらない」だけな
ので、よそのモジュールに同名が無い＝曖昧でない場合まで解決を捨てる。
宣言の有無は _MIB_LOCAL_NAME（名前 + 型キーワード）で見るので、抽出
できない右辺で宣言された節が丸ごとこの網に入る。実 MIB にある書き方
がそのまま当たる:
- `internet OBJECT IDENTIFIER ::= { iso 3 6 1 }`（複数添字）
- `acmeRoot OBJECT IDENTIFIER ::= { iso(1) org(3) ... enterprises(1)
  65001 }`（ラベル付きフルパス）

実測（現実に近い 3 ファイルの一式）: custom_mibs.json 無しで基準 52 件
に対し 49 件。directory(1.3.6.1.1) / transmission(1.3.6.1.2.1.10) /
snmpDomains(1.3.6.1.6.1) が落ちる。custom_mibs.json にベンダー根を
置くと基準 62 件に対し 50 件で、ベンダーの木が丸ごと消え、Trap の
1.3.6.1.4.1.65001.2.4.2 は 'alarmRaised' から 'acme.2.4.2' になった。
知らせの文面も事実と違い、「同じ名前が別のモジュールにもありますが」と
出るのに、別のモジュールはその名前を宣言していない。

直し方: 止めるのを「よそのモジュールが同名を宣言しているとき」だけに
絞る。曖昧でなければ、標準表 / custom_mibs.json / IMPORTS の値を
これまでどおり使う。諦めた件数の数え方にも同じ条件を足して、知らせの
文面と実態を合わせる。解決の規則が変わるので MIB_PARSER_VERSION を
上げた。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

NL = "\r\n"

# SMI 自身が internet を複数添字で宣言する（抽出から落ちる書き方）。
# internet はどの MIB ファイルも宣言していない標準表の名前
SMI_MIB = NL.join([
    "ACME-SMI DEFINITIONS ::= BEGIN",
    "internet OBJECT IDENTIFIER ::= { iso 3 6 1 }",
    "directory OBJECT IDENTIFIER ::= { internet 1 }",
    "END",
    "",
])

# ベンダー根をラベル付きフルパスで宣言する（これも抽出から落ちる）。
# 根の OID は custom_mibs.json から一意に引ける
VENDOR_MIB = NL.join([
    "ACME-ROOT-MIB DEFINITIONS ::= BEGIN",
    "acmeRoot OBJECT IDENTIFIER",
    "    ::= { iso(1) org(3) dod(6) internet(1) private(4)"
    " enterprises(1) 65001 }",
    "acmeProducts OBJECT IDENTIFIER ::= { acmeRoot 1 }",
    "acmeAlarm OBJECT IDENTIFIER ::= { acmeProducts 4 }",
    "END",
    "",
])

CUSTOM_MIBS = json.dumps({"mibs": {"1.3.6.1.4.1.65001": "acmeRoot"}},
                         ensure_ascii=False)

# 本当に曖昧な形（決定どおり諦める側）。A-MIB も B-MIB も shared を宣言
A_MIB = NL.join([
    "A-MIB DEFINITIONS ::= BEGIN",
    "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }",
    "shared OBJECT IDENTIFIER ::= { aRoot 0 1 }",
    "aAlarm OBJECT IDENTIFIER ::= { shared 1 }",
    "END",
    "",
])
B_MIB = NL.join([
    "B-MIB DEFINITIONS ::= BEGIN",
    "shared OBJECT IDENTIFIER ::= { enterprises 2222 }",
    "bAlarm OBJECT IDENTIFIER ::= { shared 9 }",
    "END",
    "",
])


class MibLocalParentGivesUpOnlyWhenAmbiguousTest(unittest.TestCase):
    def _resolver(self, files):
        """exe の隣に mibs/ を作り、files を置いて読み込む

        custom_mibs.json だけは exe の隣に置く。場合ごとに新しい一時
        フォルダを使うので、前の mib_cache.json に引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-ambig-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        mibs = os.path.join(exe_dir, "mibs")
        os.makedirs(mibs)
        for name, text in files.items():
            where = exe_dir if name == "custom_mibs.json" else mibs
            with open(os.path.join(where, name), "wb") as f:
                f.write(text.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resolver = MIBResolver()
        return resolver, out.getvalue()

    @staticmethod
    def _notices(output):
        return [line for line in output.splitlines() if "解決せず" in line]

    def test_a_standard_parent_still_resolves_when_it_is_not_ambiguous(self):
        """よそに同名が無ければ、標準表の親をこれまでどおり使うこと。"""
        resolver, _ = self._resolver({"SMI.my": SMI_MIB})
        self.assertEqual(resolver.resolve_name("directory"), "1.3.6.1.1",
                         "曖昧でないのに解決を捨てている")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.1"), "directory")

    def test_a_custom_root_still_resolves_when_it_is_not_ambiguous(self):
        """custom_mibs.json から一意に引ける親も、同じく使うこと。"""
        resolver, _ = self._resolver({"V.my": VENDOR_MIB,
                                      "custom_mibs.json": CUSTOM_MIBS})
        self.assertEqual(resolver.resolve_name("acmeProducts"),
                         "1.3.6.1.4.1.65001.1",
                         "ベンダーの木が丸ごと落ちている")
        self.assertEqual(resolver.resolve_name("acmeAlarm"),
                         "1.3.6.1.4.1.65001.1.4")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.65001.1.4"),
                         "acmeAlarm")

    def test_nothing_is_reported_when_nothing_is_ambiguous(self):
        """曖昧でないときは、諦めたという知らせを出さないこと。"""
        for label, files in (("standard", {"SMI.my": SMI_MIB}),
                             ("custom", {"V.my": VENDOR_MIB,
                                         "custom_mibs.json": CUSTOM_MIBS})):
            with self.subTest(case=label):
                _, output = self._resolver(files)
                self.assertEqual(
                    self._notices(output), [],
                    "よそのモジュールに同名が無いのに、あると知らせている")

    def test_an_ambiguous_parent_is_still_given_up(self):
        """よそに同名があるときは、決定どおり諦めること（据え置き）。"""
        resolver, output = self._resolver({"A.my": A_MIB, "B.my": B_MIB})
        self.assertIsNone(resolver.resolve_name("aAlarm"),
                          "どのモジュールの親か決めずに解決している")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.2222.1"),
                         "shared.1", "B の OID に A-MIB の名前が付いている")
        self.assertEqual(len(self._notices(output)), 1,
                         "諦めたことの知らせが 1 行ではない: %r"
                         % (output.splitlines(),))

    def test_the_two_cases_do_not_interfere(self):
        """曖昧な組と曖昧でない組が同じ mibs/ にあっても、別々に扱うこと。"""
        resolver, output = self._resolver({"A.my": A_MIB, "B.my": B_MIB,
                                           "SMI.my": SMI_MIB})
        self.assertEqual(resolver.resolve_name("directory"), "1.3.6.1.1")
        self.assertIsNone(resolver.resolve_name("aAlarm"))
        self.assertEqual(len(self._notices(output)), 1,
                         "知らせが 1 行ではない: %r" % (output.splitlines(),))


if __name__ == "__main__":
    unittest.main()
