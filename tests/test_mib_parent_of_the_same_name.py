r"""自分のモジュールが宣言した名前の親に、よその同名を借りないことを検証する。

元の不具合: 抽出は右辺が `{ 親 添字 }` ちょうどの宣言しか読まなかった。
複数添字（`{ siteRoot 3 2 }`）やラベル付きフルパス（`{ iso(1) org(3) …
enterprises(1) 777 9 }`）で宣言した節は定義の一覧から落ちて OID が
決まらず、その子は解決の「借用の回」で、内蔵表・custom_mibs.json・
よそのモジュールの同名を親にした。

実測（1.3.1）:
- 自社の木に `system ::= { iso(1) … enterprises(1) 777 9 }` を置いた MIB で、
  子の acmeSysName が標準の 1.3.6.1.2.1.1.5 になり、
  resolve_oid('1.3.6.1.2.1.1.5') が 'acmeSysName'（標準 sysName の OID に
  自社の名前）。知らせは 0 行。
- 鎖を 1 段はさむ形（system ::= { acmeRoot 1 0 }、sysName ::= { system 5 0 }）
  でも、acmeSysNameSuffix が内蔵表の sysName（1.3.6.1.2.1.1.5.0）の下の
  1.3.6.1.2.1.1.5.0.1 に付く。
- `siteMajor ::= { siteRoot 3 2 }` の子が、custom_mibs.json にある他社の
  siteMajor（1.3.6.1.4.1.777.4 / 777.3.2）の下に付く。
- IANA-MAU-MIB の `dot3MauType ::= { mib-2 snmpDot3MauMgt(26) 4 }` が
  読めず、同じ節を宣言する MAU-MIB と「曖昧」に見えて、
  dot3MauType10GigBaseCX4 など IANA にしか無い子 29 件が名前を失った
  （v1.3.0 では付いていた。resolve_oid('1.3.6.1.2.1.26.4.41') が
  'dot3MauType.41'）。

直し方: 右辺を全部読む（_parse_oid_value）。複数添字は (親, '3.2')、
ラベル付きフルパスは根からの数字 ('', '1.3.6.1.4.1.777.9') として解決へ
渡す。宣言の OID が自分の右辺で決まるので、よその同名を借りる場面そのものが
無くなる。右辺の先頭がどこにも無い（本当に決まらない）宣言を、
custom_mibs.json の同名の値で代えることはしない。名前と末尾の添字だけでは
「利用者が留めた値」と「名前が同じだけの他社や標準の節」を区別できない
ため（利用者の決定 2026-09-20『確定できないときは名前を付けない』）。
留めたいときは、足りない取り込み元の名前を custom_mibs.json に書く。
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


def mib(*lines):
    return NL.join(lines + ("END", ""))


def site(rhs, *extra):
    return mib("SITE-MIB DEFINITIONS ::= BEGIN",
               *extra,
               "siteMajor OBJECT IDENTIFIER ::= { %s }" % rhs,
               "siteMajorText OBJECT-TYPE",
               "    SYNTAX OCTET STRING",
               "    MAX-ACCESS read-only",
               "    STATUS current",
               '    DESCRIPTION "text"',
               "    ::= { siteMajor 1 }")


# 自社の木に標準名 system を置いた MIB（ラベル付きフルパス）
VENDOR_SYSTEM = mib(
    "ACME-MIB DEFINITIONS ::= BEGIN",
    "system OBJECT IDENTIFIER ::= { iso(1) org(3) dod(6) internet(1)"
    " private(4) enterprises(1) 777 9 }",
    "acmeSysName OBJECT-TYPE",
    "    SYNTAX OCTET STRING",
    "    MAX-ACCESS read-only",
    "    STATUS current",
    '    DESCRIPTION "name"',
    "    ::= { system 5 }")

# 鎖を 1 段はさむ形（複数添字）
MIRROR = mib(
    "ACME-MIRROR-MIB DEFINITIONS ::= BEGIN",
    "acmeRoot OBJECT IDENTIFIER ::= { enterprises 65011 }",
    "system OBJECT IDENTIFIER ::= { acmeRoot 1 0 }",
    "sysName OBJECT IDENTIFIER ::= { system 5 0 }",
    "acmeSysNameSuffix OBJECT IDENTIFIER ::= { sysName 1 }")

# 右辺の先頭が置いていないモジュールの名前（本当に決まらない）
STUCK_SYSTEM = mib(
    "ACME-MIB DEFINITIONS ::= BEGIN",
    "IMPORTS acmeModules FROM ACME-SMI;",
    "system OBJECT IDENTIFIER ::= { acmeModules 1 }",
    "acmeSysName OBJECT IDENTIFIER ::= { system 5 }")

SITE_ROOT = "siteRoot OBJECT IDENTIFIER ::= { enterprises 65010 }"
SITE_IMPORT = "IMPORTS siteRoot FROM SITE-SMI;"
OTHER_VENDOR = {"1.3.6.1.4.1.777.4": "siteMajor"}

ACME_TRAP = mib(
    "ACME-TRAP-MIB DEFINITIONS ::= BEGIN",
    "IMPORTS acmeModules FROM ACME-SMI;",
    "acmeTrapMIB MODULE-IDENTITY",
    '    LAST-UPDATED "202609230000Z"',
    "    ::= { acmeModules 5 }",
    "acmeTraps OBJECT IDENTIFIER ::= { acmeTrapMIB 0 }",
    "alarmRaised NOTIFICATION-TYPE",
    "    STATUS current",
    "    ::= { acmeTraps 2 }")

ACME_GRP = mib(
    "ACME-GRP-MIB DEFINITIONS ::= BEGIN",
    "IMPORTS acmeModules FROM ACME-SMI;",
    "acmeGrp OBJECT IDENTIFIER ::= { acmeModules 3 0 }",
    "acmeGrpText OBJECT IDENTIFIER ::= { acmeGrp 1 }")

# A-MIB も B-MIB も shared を宣言する。A の shared は複数添字（1.3.1 までは
# 読めなかった形。tests/test_mib_local_parent_is_not_borrowed.py の元の例）
A_MIB = mib(
    "A-MIB DEFINITIONS ::= BEGIN",
    "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }",
    "shared OBJECT IDENTIFIER ::= { aRoot 0 1 }",
    "aAlarm OBJECT IDENTIFIER ::= { shared 1 }")
B_MIB = mib(
    "B-MIB DEFINITIONS ::= BEGIN",
    "shared OBJECT IDENTIFIER ::= { enterprises 2222 }",
    "bAlarm OBJECT IDENTIFIER ::= { shared 9 }")

# 同じ節を 2 つのモジュールが同じ OID で宣言（片方はラベル付き）。
# 実 MIB の MAU-MIB と IANA-MAU-MIB の dot3MauType と同じ形
MAU = mib(
    "MAU-MIB DEFINITIONS ::= BEGIN",
    "snmpDot3MauMgt OBJECT IDENTIFIER ::= { mib-2 26 }",
    "dot3MauType OBJECT IDENTIFIER ::= { snmpDot3MauMgt 4 }",
    "dot3MauType10M OBJECT-IDENTITY",
    "    STATUS current",
    "    ::= { dot3MauType 1 }")
IANA_MAU = mib(
    "IANA-MAU-MIB DEFINITIONS ::= BEGIN",
    "dot3MauType OBJECT IDENTIFIER ::= { mib-2 snmpDot3MauMgt(26) 4 }",
    "dot3MauType10M OBJECT-IDENTITY",
    "    STATUS current",
    "    ::= { dot3MauType 1 }",
    "dot3MauType10GigBaseCX4 OBJECT-IDENTITY",
    "    STATUS current",
    "    ::= { dot3MauType 41 }")


class ParentOfTheSameNameTest(unittest.TestCase):
    def _resolver(self, files, custom=None):
        """exe の隣に mibs/ を作り、files と custom_mibs.json を置いて読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-same-name-")
        self.addCleanup(shutil.rmtree, exe_dir, True)
        exe = os.path.join(exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        mibs = os.path.join(exe_dir, "mibs")
        os.makedirs(mibs)
        for name, text in files.items():
            with open(os.path.join(mibs, name), "wb") as f:
                f.write(text.encode("utf-8"))
        if custom is not None:
            with open(os.path.join(exe_dir, "custom_mibs.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"mibs": custom}, f)
        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resolver = MIBResolver()
        return resolver, out.getvalue()

    @staticmethod
    def _notices(output):
        return [line for line in output.splitlines() if "解決せず" in line]

    # --- 元の不具合: 名前が同じだけの節を親にしない ------------------
    def test_a_vendor_system_is_not_the_standard_system(self):
        """ラベル付きフルパスの自社 system の子を、標準 system に付けないこと。"""
        r, _ = self._resolver({"A.my": VENDOR_SYSTEM})
        self.assertNotEqual(r.resolve_oid("1.3.6.1.2.1.1.5"), "acmeSysName",
                            "標準の sysName の OID に自社の名前が付いている")
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.777.9.5"), "acmeSysName")

    def test_one_more_link_in_the_chain_changes_nothing(self):
        """鎖を 1 段はさんでも、内蔵表の sysName の下に付けないこと。"""
        r, _ = self._resolver({"A.my": MIRROR})
        self.assertNotEqual(r.resolve_oid("1.3.6.1.2.1.1.5.0.1"),
                            "acmeSysNameSuffix",
                            "内蔵表の sysName の下に自社の名前が付いている")
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.65011.1.0.5.0.1"),
                         "acmeSysNameSuffix")

    def test_another_vendors_custom_name_is_not_the_parent(self):
        """複数添字の節の子を、custom_mibs.json の他社の同名に付けないこと。"""
        r, _ = self._resolver({"S.my": site("siteRoot 3 2", SITE_ROOT)},
                              custom=OTHER_VENDOR)
        self.assertNotEqual(r.resolve_oid("1.3.6.1.4.1.777.4.1"),
                            "siteMajorText",
                            "custom_mibs.json の他社の OID に子が付いている")
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.65010.3.2.1"),
                         "siteMajorText")

    def test_a_matching_last_number_does_not_make_the_same_name_the_parent(
            self):
        """本当に決まらない節を、末尾が一致する custom の同名で代えないこと。"""
        # 右辺の先頭（siteRoot / siteMid の根）がどこにも無い。
        # custom_mibs.json の同名の OID は末尾が右辺の添字と一致するが、
        # 他社や標準の節かもしれない
        cases = (
            ("one number", site("siteRoot 4", SITE_IMPORT), OTHER_VENDOR,
             "1.3.6.1.4.1.777.4.1"),
            ("standard tree", site("siteRoot 4", SITE_IMPORT),
             {"1.3.6.1.2.1.4": "siteMajor"}, "1.3.6.1.2.1.4.1"),
            ("two numbers", site("siteRoot 3 2", SITE_IMPORT),
             {"1.3.6.1.4.1.777.3.2": "siteMajor"}, "1.3.6.1.4.1.777.3.2.1"),
            ("one link more", site(
                "siteMid 4", SITE_IMPORT,
                "siteMid OBJECT IDENTIFIER ::= { siteRoot 7 }"),
             OTHER_VENDOR, "1.3.6.1.4.1.777.4.1"),
        )
        for label, text, custom, child in cases:
            with self.subTest(label):
                r, _ = self._resolver({"S.my": text}, custom=custom)
                self.assertNotEqual(r.resolve_oid(child), "siteMajorText")
                self.assertIsNone(r.resolve_name("siteMajorText"))

    def test_a_stuck_declaration_does_not_take_the_standard_name(self):
        """右辺が決まらない自社 system の子を、標準 system に付けないこと。"""
        r, _ = self._resolver({"A.my": STUCK_SYSTEM})
        self.assertNotEqual(r.resolve_oid("1.3.6.1.2.1.1.5"), "acmeSysName")
        self.assertIsNone(r.resolve_name("acmeSysName"))

    # --- 留めたいときは、足りない取り込み元を custom_mibs.json に書く ----
    def test_pinning_the_missing_import_names_the_children(self):
        """足りない取り込み元を留めれば、単添字・複数添字とも子に名前が付くこと。"""
        pin = {"1.3.6.1.4.1.65001.3": "acmeModules"}
        for label, text, child, name in (
                ("one number", ACME_TRAP, "1.3.6.1.4.1.65001.3.5.0.2",
                 "alarmRaised"),
                ("two numbers", ACME_GRP, "1.3.6.1.4.1.65001.3.3.0.1",
                 "acmeGrpText")):
            with self.subTest(label):
                r, _ = self._resolver({"M.my": text}, custom=pin)
                self.assertEqual(r.resolve_oid(child), name)

    # --- 読めるようになった右辺は、自分のモジュールの OID に付く ------
    def test_a_multi_number_parent_names_its_own_modules_oid(self):
        """複数添字の親の子は、自分のモジュールの OID に付き、知らせも出ないこと。"""
        r, output = self._resolver({"A.my": A_MIB, "B.my": B_MIB})
        self.assertEqual(r.resolve_name("aAlarm"), "1.3.6.1.4.1.1111.0.1.1")
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.1111.0.1.1"), "aAlarm")
        self.assertNotEqual(r.resolve_oid("1.3.6.1.4.1.2222.1"), "aAlarm",
                            "B の OID に A-MIB の名前が付いている")
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.2222.9"), "bAlarm")
        self.assertEqual(self._notices(output), [],
                         "決まる親なのに諦めたと知らせている")

    def test_the_same_node_declared_twice_keeps_its_children(self):
        """同じ節を 2 モジュールが同じ OID で宣言しても、子の名前が消えないこと。"""
        r, output = self._resolver({"MAU.my": MAU, "IANA.my": IANA_MAU})
        self.assertEqual(r.resolve_oid("1.3.6.1.2.1.26.4.41"),
                         "dot3MauType10GigBaseCX4")
        self.assertEqual(r.resolve_oid("1.3.6.1.2.1.26.4.1"),
                         "dot3MauType10M")
        self.assertEqual(self._notices(output), [])

    def test_the_value_forms_that_are_read(self):
        """右辺の書き方ごとに、その OID に名前が付くこと。"""
        for rhs, oid in (
                ("aRoot 0 1", "1.3.6.1.4.1.1111.0.1"),
                ("iso 3 6 1 4 1 1111 7", "1.3.6.1.4.1.1111.7"),
                ("iso(1) org(3) dod(6) internet(1) private(4)"
                 " enterprises(1) 1111 8", "1.3.6.1.4.1.1111.8"),
                ("aRoot sub(3) 4", "1.3.6.1.4.1.1111.3.4"),
                ("1 3 6 1 4 1 1111 9", "1.3.6.1.4.1.1111.9"),
                ("\r\n    aRoot\r\n    5 6\r\n  ", "1.3.6.1.4.1.1111.5.6")):
            with self.subTest(rhs=rhs):
                text = mib("FORM-MIB DEFINITIONS ::= BEGIN",
                           "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }",
                           "formNode OBJECT IDENTIFIER ::= {%s}" % rhs,
                           "formLeaf OBJECT IDENTIFIER ::= { formNode 1 }")
                r, _ = self._resolver({"F.my": text})
                self.assertEqual(r.resolve_name("formNode"), oid)
                self.assertEqual(r.resolve_oid(oid + ".1"), "formLeaf")

    def test_an_unreadable_value_still_does_not_borrow(self):
        """読めない右辺（2 つ目以降に裸の名前）の節の子も、よその同名に付けないこと。"""
        text = mib("A-MIB DEFINITIONS ::= BEGIN",
                   "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }",
                   "shared OBJECT IDENTIFIER ::= { aRoot sub 1 }",
                   "aAlarm OBJECT IDENTIFIER ::= { shared 1 }")
        r, output = self._resolver({"A.my": text, "B.my": B_MIB})
        self.assertNotEqual(r.resolve_oid("1.3.6.1.4.1.2222.1"), "aAlarm",
                            "B の OID に A-MIB の名前が付いている")
        self.assertIsNone(r.resolve_name("aAlarm"))
        self.assertEqual(len(self._notices(output)), 1,
                         "諦めたことの知らせが 1 行ではない: %r"
                         % (output.splitlines(),))


if __name__ == "__main__":
    unittest.main()
