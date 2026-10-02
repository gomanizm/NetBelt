"""SNMP の機能が使う依存（pysnmp・pyasn1・cryptography）が、実際に読み込めることを確かめる。

なぜ要るか: src/core/snmp_manager.py は pysnmp の import を try で囲み、
失敗すると _PYSNMP_AVAILABLE = False にして、アプリは何事も無く起動する。
GET/WALK/Trap は「SNMPライブラリ(pysnmp)を利用できません」を返すだけに
なるので、起動しただけでは壊れていることが分からない。

実際に起きたこと: pyasn1 の脆弱性（GHSA-8ppf-4f7h-5ppj / CVE-2026-59885。
0.6.3 以下が対象で 0.6.4 で修正）に対応するため pyasn1 を 0.6.4 に上げると、
pysnmp 5.1.0 は pyasn1.compat.octets（0.6.4 で消えた）を import できず、
依存の解決は通るのに SNMP だけが黙って使えなくなった。そこで pysnmp を
7 系（asyncio 版の hlapi）へ移した。

もう 1 つ実際に起きたこと: 最初に選んだ pysnmp 7.1.30 の AES（rfc3826 の
aes。AES-192/256 もこれを継ぐ）は cryptography.hazmat.decrepit.ciphers.modes
から CFB を読む。このモジュールは cryptography 47.0.0 で入ったもので、
据え置いた 46.0.3 には無い。読めないと pysnmp は例外を出さず、モジュールの
PysnmpCryptoError を True にするだけで、v3 の AES は送受信とも「Ciphering
services not available」になった（実測）。pip check はこれを拾わない
（pysnmp の METADATA は cryptography を dev の extra にしか書いていない）。
そこで pysnmp を 7.1.28 にした（7.1.28 の AES は
cryptography.hazmat.primitives.ciphers の modes を読む）。

何を確かめるか:
  - エンジン・UDP（asyncio）・Trap 受信の部品と、snmp_manager が使う
    高水準 API の名前が読み込めること。非推奨の別名に頼っていないこと
  - core.snmp_manager の _PYSNMP_AVAILABLE が True であること。
    DeprecationWarning を例外にしても True のままであること
  - pyasn1 が CVE-2026-59885 の修正版（0.6.4）以上であること
  - v3 の暗号（DES・3DES・AES）の各モジュールの PysnmpCryptoError が
    False であること（cryptography を読めている）。落ちたときの手がかりに、
    そのモジュールが try の中で読む cryptography の部品を pysnmp のソース
    から拾って読み直し、読めないものを示す。モジュール名は版ごとに違う
    （7.1.30 と 7.1.28 で違った）ので決め打ちしない

import の失敗は skip にしない。ここが落ちること自体が知らせたい事実である。
"""
import ast
import importlib
import inspect
import os
import re
import subprocess
import sys
import unittest
import warnings

sys.path.insert(0, "src")

# リポジトリの src（子プロセスへ渡す。起動したときの作業フォルダによらない）
SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "src")

# snmp_manager とテストの相手役が使う pysnmp 7 の高水準 API の名前
HLAPI_NAMES = (
    "SnmpEngine", "CommunityData", "UsmUserData", "UdpTransportTarget",
    "ContextData", "ObjectType", "ObjectIdentity", "NotificationType",
    "get_cmd", "walk_cmd", "send_notification",
)
# resolve_v3_protocols の表が引く USM の定数（UI の全選択肢）と、
# 取り違えの確認に使う Blumenthal 版。7 系の名前（usm... は非推奨の別名）
USM_PROTOCOL_NAMES = (
    "USM_AUTH_NONE", "USM_AUTH_HMAC96_MD5", "USM_AUTH_HMAC96_SHA",
    "USM_AUTH_HMAC128_SHA224", "USM_AUTH_HMAC192_SHA256",
    "USM_AUTH_HMAC256_SHA384", "USM_AUTH_HMAC384_SHA512",
    "USM_PRIV_NONE", "USM_PRIV_CBC56_DES", "USM_PRIV_CBC168_3DES",
    "USM_PRIV_CFB128_AES", "USM_PRIV_CFB192_AES", "USM_PRIV_CFB256_AES",
    "USM_PRIV_CFB192_AES_BLUMENTHAL", "USM_PRIV_CFB256_AES_BLUMENTHAL",
)
# エンジン・トランスポート・Trap 受信（SNMPTrapReceiver が組む部品）
ENGINE_MODULES = (
    "pysnmp.hlapi.v3arch.asyncio",
    "pysnmp.entity.engine",
    "pysnmp.entity.config",
    "pysnmp.carrier.asyncio.dgram.udp",
    "pysnmp.carrier.asyncio.dispatch",
    "pysnmp.entity.rfc3413.ntfrcv",
)
# v3 の暗号（privProtocol）を実装するモジュール。それぞれ cryptography を
# 読めないと PysnmpCryptoError = True になり、暗号化も復号もできない。
# AES-192/256（pysnmp.proto.secmod.eso.priv.aes192 / aes256）は rfc3826 の
# aes の暗号処理を継ぐので、aes の PysnmpCryptoError に従う
CIPHER_MODULES = (
    ("DES", "pysnmp.proto.secmod.rfc3414.priv.des"),
    ("3DES", "pysnmp.proto.secmod.eso.priv.des3"),
    ("AES-128/192/256", "pysnmp.proto.secmod.rfc3826.priv.aes"),
)
# CVE-2026-59885 を直した pyasn1 の版
PYASN1_FIXED = (0, 6, 4)


def _version_tuple(text):
    """「0.6.4」のような版の文字列を比べられる組にする"""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        raise AssertionError("版の文字列を読めない: %r" % (text,))
    return tuple(int(part) for part in match.groups())


def _import_failures(names):
    """読み込めなかったモジュールと理由の一覧（全部読めれば空）"""
    failures = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as e:
            failures.append("%s: %s: %s" % (name, type(e).__name__, e))
    return failures


def _import_cipher(name):
    """pysnmp の暗号モジュールを読む。

    3DES・AES のモジュールを pysnmp の中で最初に読むと、pysnmp 7.1.28 では
    循環 import で AttributeError になる（rfc3414 の service が eso の
    aes192 / aes256 / des3 を読み返すため。実測）。アプリと同じくエンジンを
    先に読んでおく（ここで落ちるのは、テストを 1 つだけ流したときの見かけの
    失敗になる）。
    """
    importlib.import_module("pysnmp.entity.engine")
    return importlib.import_module(name)


def _guarded_cryptography_imports(module):
    """module が「try: import … except ImportError」の中で読む cryptography の部品。

    pysnmp の暗号モジュールは、この try が失敗すると PysnmpCryptoError を
    True にする。ソースから拾うので、版ごとのモジュール名を決め打ちしない。

    Returns:
        [(モジュール名, 名前), ...]
    """
    tree = ast.parse(inspect.getsource(module))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.Try):
            continue
        for statement in node.body:
            if (isinstance(statement, ast.ImportFrom) and statement.module
                    and statement.module.split(".")[0] == "cryptography"):
                found.extend((statement.module, alias.name)
                             for alias in statement.names)
    return found


def _unimportable(parts):
    """(モジュール名, 名前) のうち、読めないものと理由の一覧"""
    failures = []
    for module_name, name in parts:
        try:
            getattr(importlib.import_module(module_name), name)
        except Exception as e:
            failures.append("from %s import %s: %s: %s"
                            % (module_name, name, type(e).__name__, e))
    return failures


class SnmpLibraryAvailableTest(unittest.TestCase):

    def test_the_engine_and_trap_receiver_modules_import(self):
        """エンジン・UDP（asyncio）・Trap 受信の部品が読み込めること。"""
        self.assertEqual(_import_failures(ENGINE_MODULES), [])

    def test_the_hlapi_names_exist(self):
        """GET/WALK/Trap に使う高水準 API の名前がそろっていること。

        旧名（getCmd など）や DeprecationWarning を出す別名に頼っていないか
        も見る。pysnmp 7 の旧名は __getattr__ で警告を出して新名へ転送する。
        """
        hlapi = importlib.import_module("pysnmp.hlapi.v3arch.asyncio")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            missing = [name for name in HLAPI_NAMES + USM_PROTOCOL_NAMES
                       if not hasattr(hlapi, name)]
        self.assertEqual(missing, [])

    def test_the_request_functions_are_asynchronous(self):
        """7 系の形（async）であること。5.1.0 の同期ジェネレータとは呼び方が違う。"""
        from pysnmp.hlapi.v3arch.asyncio import (
            UdpTransportTarget, get_cmd, send_notification, walk_cmd)
        self.assertTrue(inspect.iscoroutinefunction(UdpTransportTarget.create))
        self.assertTrue(inspect.iscoroutinefunction(get_cmd))
        self.assertTrue(inspect.iscoroutinefunction(send_notification))
        self.assertTrue(inspect.isasyncgenfunction(walk_cmd))

    def test_the_snmpv3_ciphers_can_load_cryptography(self):
        """DES・3DES・AES の各モジュールが cryptography を読み込めていること。

        読めないと pysnmp は例外を出さず PysnmpCryptoError を True にし、
        その方式の v3 は送受信とも失敗する（UI から選べるのに使えない）。
        """
        broken = []
        for label, name in CIPHER_MODULES:
            module = _import_cipher(name)
            # 属性が無いのも「読めていない」として扱う（成功扱いにしない）
            if getattr(module, "PysnmpCryptoError", True) is not False:
                broken.append("%s（%s）: %s" % (
                    label, name,
                    _unimportable(_guarded_cryptography_imports(module))
                    or "読めない部品は見つからない"))
        cryptography = importlib.import_module("cryptography")
        self.assertEqual(
            broken, [],
            "v3 の暗号に使う cryptography を読み込めていない方式がある"
            "（cryptography %s）" % cryptography.__version__)

    def test_the_cryptography_parts_used_by_the_ciphers_import(self):
        """各暗号モジュールが try の中で読む cryptography の部品が、それぞれ読めること。

        部品が 1 つも見つからないときも失敗にする（pysnmp の作りが変わって、
        ここの確かめが空振りしている）。
        """
        for label, name in CIPHER_MODULES:
            with self.subTest(cipher=label):
                parts = _guarded_cryptography_imports(_import_cipher(name))
                self.assertTrue(parts, "%s が cryptography から読む部品を見つけられない"
                                % name)
                self.assertEqual(_unimportable(parts), [])

    def test_snmp_manager_reports_the_library_available(self):
        """core.snmp_manager の import が通り、_PYSNMP_AVAILABLE が True であること。

        False のままだとアプリは起動するが SNMP は全部「利用できません」になる。
        """
        import core.snmp_manager as snmp_manager
        self.assertTrue(
            snmp_manager._PYSNMP_AVAILABLE,
            "snmp_manager が pysnmp を読み込めていない。読めないモジュール: %s"
            % (_import_failures(ENGINE_MODULES) or "（ここで調べた範囲には無い）"))

    def test_snmp_manager_does_not_import_deprecated_names(self):
        """DeprecationWarning を例外にしても、_PYSNMP_AVAILABLE が True であること。

        snmp_manager の import は try で囲んであり、非推奨の名前を読んで
        警告が例外になると、黙って SNMP が使えなくなる。pysnmp が次の版で
        別名を消したときに同じことが起きるので、別名に頼っていないことを
        まっさらな子プロセスで確かめる（このプロセスは読み込み済み）。
        """
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "import core.snmp_manager as m; "
                "sys.exit(0 if m._PYSNMP_AVAILABLE else 3)")
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                   PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, "-W", "error::DeprecationWarning", "-c", code, SRC_DIR],
            capture_output=True, env=env, timeout=120)
        self.assertEqual(
            result.returncode, 0,
            "DeprecationWarning を例外にすると snmp_manager が pysnmp を読めない"
            "（終了コード %d）: %s" % (result.returncode,
                                      result.stderr.decode("utf-8", "replace")[-2000:]))

    def test_pyasn1_is_the_fixed_release(self):
        """pyasn1 が CVE-2026-59885 の修正版（0.6.4）以上であること。"""
        import pyasn1
        import pysnmp
        self.assertGreaterEqual(
            _version_tuple(pyasn1.__version__), PYASN1_FIXED,
            "pyasn1 %s は GHSA-8ppf-4f7h-5ppj / CVE-2026-59885 の対象"
            "（pysnmp %s）" % (pyasn1.__version__, pysnmp.__version__))


if __name__ == "__main__":
    unittest.main()
