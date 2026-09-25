r"""自分のモジュールが宣言している親を、よそのモジュールから借りないことを検証する。

親は同じモジュールの定義を優先し、同じモジュールに無い親（IMPORTS）だけ
モジュールをまたいで探す。その「同じモジュールに宣言があるか」を、抽出
できた定義の名前だけで見ていた。抽出は `::= { 親 添字 }` の形しか拾わない
ので、実 MIB にある複数添字の右辺（`::= { aRoot 0 1 }`）で宣言された親は
その一覧から落ち、同じモジュールの宣言が無かったことにされる。

実測（mibs/ に 2 ファイル）:
  A.my: A-MIB / aRoot ::= { enterprises 1111 } /
        shared ::= { aRoot 0 1 } / aAlarm ::= { shared 1 }
  B.my: B-MIB / shared ::= { enterprises 2222 } / bAlarm ::= { shared 9 }
A-MIB 自身が shared を宣言しているのに抽出から落ちるため、全モジュール
共通の表にある B-MIB の shared が親になり、A-MIB の aAlarm が B-MIB の
名前空間へ入る（resolve_oid('1.3.6.1.4.1.2222.1') が 'aAlarm'）。
「解決できない」で済まず、B ベンダーの OID で来た Trap に他社の名前が
出るので、画面の情報が嘘になる。

利用者の決定（2026-09-20）: これは解決しない。どのモジュールの親か
確定できないときは名前を付けず OID のまま出す（誤った他社の名前を
出すより安全）。判断に使った条件と、解決を諦めた件数を標準出力に
1 行残す。

直し方: モジュールが宣言している名前を、抽出できた定義だけでなく
「名前 + 型キーワード」の並びからも集める。そのモジュールが宣言して
いる親は、そのモジュールで OID が決まるまで使わないので、よその同名で
埋めることがなくなる。決まらなければその定義は解決しないまま残る。
抽出・解決の規則が変わるので MIB_PARSER_VERSION を上げた。
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

NL = "\r\n"

# A-MIB の shared は複数添字なので抽出から落ちる。B-MIB も shared を持つ
A_MIB = ("A-MIB DEFINITIONS ::= BEGIN" + NL
         + "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }" + NL
         + "shared OBJECT IDENTIFIER ::= { aRoot 0 1 }" + NL
         + "aAlarm OBJECT IDENTIFIER ::= { shared 1 }" + NL
         + "END" + NL)

B_MIB = ("B-MIB DEFINITIONS ::= BEGIN" + NL
         + "shared OBJECT IDENTIFIER ::= { enterprises 2222 }" + NL
         + "bAlarm OBJECT IDENTIFIER ::= { shared 9 }" + NL
         + "END" + NL)

# 親を自分では宣言せず IMPORTS するモジュール（対照）
SMI_MIB = ("VENDOR-SMI DEFINITIONS ::= BEGIN" + NL
           + "vendorRoot OBJECT IDENTIFIER ::= { enterprises 3333 }" + NL
           + "vendorMgmt OBJECT IDENTIFIER ::= { vendorRoot 9 }" + NL
           + "END" + NL)

USE_MIB = ("VENDOR-USE-MIB DEFINITIONS ::= BEGIN" + NL
           + "IMPORTS vendorMgmt FROM VENDOR-SMI;" + NL
           + "useNode OBJECT IDENTIFIER ::= { vendorMgmt 4 }" + NL
           + "END" + NL)


class MibLocalParentIsNotBorrowedTest(unittest.TestCase):
    def _resolver(self, files):
        """exe の隣に mibs/ を作り、files（名前→中身）を置いて読み込む

        標準出力は取っておき、知らせの 1 行を確かめるのに使う。場合ごとに
        新しい一時フォルダを使うので、前の mib_cache.json に引きずられない。
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-borrow-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(exe_dir, "mibs"))
        for name, text in files.items():
            with open(os.path.join(exe_dir, "mibs", name), "wb") as f:
                f.write(text.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resolver = MIBResolver()
        return resolver, out.getvalue()

    def test_another_modules_oid_does_not_get_this_modules_name(self):
        """B ベンダーの OID に A-MIB の名前が付かないこと。"""
        resolver, _ = self._resolver({"A.my": A_MIB, "B.my": B_MIB})
        self.assertNotEqual(resolver.resolve_oid("1.3.6.1.4.1.2222.1"),
                            "aAlarm",
                            "B の OID に A-MIB の名前が付いている")
        self.assertNotIn("aAlarm", resolver.oid_to_name.values())

    def test_the_unresolvable_definition_stays_unnamed(self):
        """親を決められない定義は、名前を付けずに残すこと。"""
        resolver, _ = self._resolver({"A.my": A_MIB, "B.my": B_MIB})
        self.assertIsNone(resolver.resolve_name("aAlarm"),
                          "どのモジュールの親か決めずに解決している")

    def test_the_other_definitions_still_resolve(self):
        """巻き添えで他の定義まで落ちないこと。"""
        resolver, _ = self._resolver({"A.my": A_MIB, "B.my": B_MIB})
        self.assertEqual(resolver.resolve_name("aRoot"), "1.3.6.1.4.1.1111")
        self.assertEqual(resolver.resolve_name("shared"), "1.3.6.1.4.1.2222")
        self.assertEqual(resolver.resolve_name("bAlarm"),
                         "1.3.6.1.4.1.2222.9")

    def test_giving_up_is_reported_on_stdout(self):
        """諦めた件数と、その判断に使った条件を 1 行知らせること。"""
        _, output = self._resolver({"A.my": A_MIB, "B.my": B_MIB})
        lines = [line for line in output.splitlines()
                 if "1件" in line and "モジュール" in line
                 and "OID" in line]
        self.assertEqual(len(lines), 1,
                         "諦めたことの知らせが 1 行ではない: %r"
                         % (output.splitlines(),))

    def test_nothing_is_reported_when_nothing_is_given_up(self):
        """諦めるものが無いときは、その知らせを出さないこと。"""
        _, output = self._resolver({"B.my": B_MIB})
        self.assertNotIn("解決せず", output,
                         "諦めていないのに知らせが出ている: %r" % (output,))

    def test_an_imported_parent_from_another_module_still_resolves(self):
        """自分で宣言していない親は、これまでどおり越えて解決すること。"""
        resolver, _ = self._resolver({"SMI.my": SMI_MIB, "USE.my": USE_MIB})
        self.assertEqual(resolver.resolve_name("useNode"),
                         "1.3.6.1.4.1.3333.9.4",
                         "IMPORTS した親を越えて解決できない")


if __name__ == "__main__":
    unittest.main()
