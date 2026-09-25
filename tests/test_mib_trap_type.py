r"""SMIv1 の TRAP-TYPE（RFC 1215）の Trap に名前が付くことを検証する。

SMIv1 のベンダー MIB は Trap を
    acmeAlarm TRAP-TYPE
        ENTERPRISE acme
        ::= 7
の形で書く。v1 Trap（enterprise=acme、specific=7）は、受信側で
RFC 3584 の変換どおり snmpTrapOID = <acme の OID>.0.7 として届く
（pysnmp も同じ OID を置く）。

実測（1.3.1、mibs/ に acme ::= { enterprises 99999 } と上の Trap を置く）:
resolve_oid('1.3.6.1.4.1.99999.0.7') が 'acme.0.7' になり、Trap 一覧に
名前が出ない。同じ形を SMIv2 の NOTIFICATION-TYPE で書いた MIB は
'acme2Alarm' と出る。抽出の型キーワードの並びに TRAP-TYPE はあったが
（定義の境目の判定にだけ使う）、抽出する定義の種類には無く、`::= 7` を
ENTERPRISE.0.7 に読み替える処理も無かった。

直し方: 「名前 TRAP-TYPE ENTERPRISE 親 … ::= 番号」を抽出し、
(名前, ENTERPRISE, '0.' + 番号) の定義として解決へ渡す。抽出の規則が
変わるので MIB_PARSER_VERSION を上げた（上げないと 1.3.1 が作った
mib_cache.json が使われ続け、Trap の名前が出ないまま）。
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
    return NL.join(lines + ("",))


V1_MIB = mib(
    "ACME-V1-MIB DEFINITIONS ::= BEGIN",
    "IMPORTS",
    "    enterprises FROM RFC1155-SMI",
    "    OBJECT-TYPE FROM RFC-1212",
    "    TRAP-TYPE FROM RFC-1215;",
    "acme OBJECT IDENTIFIER ::= { enterprises 99999 }",
    "acmeObjects OBJECT IDENTIFIER ::= { acme 1 }",
    "acmeAlarmText OBJECT-TYPE",
    "    SYNTAX DisplayString",
    "    ACCESS read-only",
    "    STATUS mandatory",
    '    DESCRIPTION "text of the alarm ::= { acme 42 }"',
    "    ::= { acmeObjects 1 }",
    "acmeAlarm TRAP-TYPE",
    "    ENTERPRISE acme",
    "    VARIABLES { acmeAlarmText }",
    '    DESCRIPTION',
    '        "An alarm was raised. See RFC 1215: ::= 99"',
    "    ::= 7",
    "acmeClear TRAP-TYPE",
    "    ENTERPRISE acme",
    "    VARIABLES { acmeAlarmText }",
    "    ::= 8",
    "acmeStatus OBJECT IDENTIFIER ::= { acmeObjects 2 }",
    "END")

# 同じ Trap を SMIv2 で書いたもの（対照）
V2_MIB = mib(
    "ACME-V2-MIB DEFINITIONS ::= BEGIN",
    "IMPORTS enterprises, NOTIFICATION-TYPE FROM SNMPv2-SMI;",
    "acme2 OBJECT IDENTIFIER ::= { enterprises 99998 }",
    "acme2Traps OBJECT IDENTIFIER ::= { acme2 0 }",
    "acme2Alarm NOTIFICATION-TYPE",
    "    STATUS current",
    '    DESCRIPTION "alarm"',
    "    ::= { acme2Traps 7 }",
    "END")

# ENTERPRISE の節が別のファイルにある形
V1_SMI = mib(
    "ACME-V1-SMI DEFINITIONS ::= BEGIN",
    "acmeProducts OBJECT IDENTIFIER ::= { enterprises 99997 }",
    "END")
V1_USE = mib(
    "ACME-V1-USE-MIB DEFINITIONS ::= BEGIN",
    "IMPORTS acmeProducts FROM ACME-V1-SMI TRAP-TYPE FROM RFC-1215;",
    "acmeLinkLost TRAP-TYPE",
    "    ENTERPRISE acmeProducts",
    "    ::= 3",
    "END")


class MibTrapTypeTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ を置く凍結ビルドとして動かす。場合ごとに新しい
        # 一時フォルダを使うので、前の mib_cache.json に引きずられない
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-trap-type-")
        self.addCleanup(shutil.rmtree, self.exe_dir, True)
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        self.mibs = os.path.join(self.exe_dir, "mibs")
        os.makedirs(self.mibs)

    def _resolver(self, files):
        for name, text in files.items():
            with open(os.path.join(self.mibs, name), "wb") as f:
                f.write(text.encode("utf-8"))
        from core.mib_resolver import MIBResolver
        with contextlib.redirect_stdout(io.StringIO()):
            return MIBResolver()

    def test_an_smiv1_trap_type_is_named(self):
        """v1 Trap が届く ENTERPRISE.0.N に、TRAP-TYPE の名前が付くこと。"""
        r = self._resolver({"V1.my": V1_MIB, "V2.my": V2_MIB})
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.99999.0.7"), "acmeAlarm")
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.99999.0.8"), "acmeClear")
        self.assertEqual(r.resolve_name("acmeAlarm"), "1.3.6.1.4.1.99999.0.7")
        # 対照: SMIv2 の同じ形はもともと名前が付く
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.99998.0.7"), "acme2Alarm")

    def test_the_neighbouring_definitions_are_untouched(self):
        """TRAP-TYPE の前後の定義が、これまでどおり解決すること。"""
        r = self._resolver({"V1.my": V1_MIB})
        self.assertEqual(r.resolve_name("acmeAlarmText"),
                         "1.3.6.1.4.1.99999.1.1")
        self.assertEqual(r.resolve_name("acmeStatus"),
                         "1.3.6.1.4.1.99999.1.2")
        # DESCRIPTION の中の `::= 99` や `::= { acme 42 }` を拾わない
        self.assertNotIn("1.3.6.1.4.1.99999.0.99", r.oid_to_name)
        self.assertNotIn("1.3.6.1.4.1.99999.42", r.oid_to_name)

    def test_the_enterprise_may_live_in_another_file(self):
        """ENTERPRISE の節が別のファイルにあっても解決すること。"""
        r = self._resolver({"SMI.my": V1_SMI, "USE.my": V1_USE})
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.99997.0.3"),
                         "acmeLinkLost")

    def test_a_cache_from_the_previous_parser_is_rebuilt(self):
        """1.3.1 の解析器が作ったキャッシュを使い続けないこと。

        1.3.1 の解析器（2026-09-23.2）は TRAP-TYPE を拾わないので、その
        キャッシュには Trap の名前が無い。版を上げないと、アプリを更新
        しても mibs/ を変えない限り名前が出ない。
        """
        self._resolver({"V1.my": V1_MIB})
        cache_path = os.path.join(self.exe_dir, "mib_cache.json")
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        # 1.3.1 が書いたのと同じ形にする（版と、Trap の名前が無い mibs）
        cache["parser"] = "2026-09-23.2"
        cache["mibs"] = {oid: name for oid, name in cache["mibs"].items()
                         if name not in ("acmeAlarm", "acmeClear")}
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        from core.mib_resolver import MIBResolver
        with contextlib.redirect_stdout(io.StringIO()):
            r = MIBResolver()
        self.assertEqual(r.resolve_oid("1.3.6.1.4.1.99999.0.7"), "acmeAlarm",
                         "1.3.1 のキャッシュがそのまま使われている")


if __name__ == "__main__":
    unittest.main()
