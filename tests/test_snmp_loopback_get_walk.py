"""SNMP の GET/WALK を、127.0.0.1 の実際の応答側に向けて動かして確かめる。

なぜ要るか: pyasn1 の脆弱性（GHSA-8ppf-4f7h-5ppj / CVE-2026-59885）対応で、
pysnmp を 5.1.0 から 7.1.28 へ移した。7 系は asyncore が無く asyncio だけに
なり、getCmd / nextCmd の同期の呼び出しは無くなった。それまでの GET/WALK の
テストは getCmd / nextCmd を差し替える mock が中心で、BER の符号化・復号も
USM の鍵の導出も一度も通っていなかった。移行で壊れても mock のテストは
通ってしまう。

何を確かめるか: tests/snmp_loopback_agent.py の応答側（pysnmp 7.1.28 だけで
組んだコマンドレスポンダ。専用のスレッドとイベントループで 127.0.0.1 の
空きポートに立てる）に、NetBelt の SNMPManager（公開のメソッドと
シグナルだけ）で問い合わせる。
  - v1・v2c・v3（noAuthNoPriv / authNoPriv / authPriv の代表）の GET で、
    OID・型名・値の文字列が 5.1.0 と同じであること。期待値は、同じ応答側へ
    5.1.0 の NetBelt（d2b37cc の SNMPWorker と pysnmp 5.1.0）で問い合わせた
    実測値
  - 存在しない OID: v2c/v3 は noSuchInstance / noSuchObject を行として返し、
    v1 は noSuchName のエラーになる
  - WALK が部分木の外の隣の値を拾わない、endOfMibView で止まる、
    100 件ごとに進捗を出す
  - OID が増えない応答（同じ OID を返す・前へ戻る機器）で WALK が回り
    続けず、5.1.0 と同じく「OID not increasing」の途中まで（行が無ければ
    エラー）になる（7.1.28 の GETNEXT はこれを検出しないので NetBelt が見る）
  - WALK の応答に noSuchObject / noSuchInstance を返す機器で、5.1.0 と同じく
    そこで黙って終わる。素の NULL は 5.1.0 と同じく「OID が増えない」を
    比べたあとで終わる。開始 OID を記号名で書いた WALK が、数字で書いたときと
    同じ行になる
  - 既定で読む MIB のオブジェクトを機器が MIB と違う型で返しても、GET が
    失敗せず WALK も途切れず、表示が 5.1.0 と同じ（期待値は 5.1.0 の実測）
  - 応答しない機器では既定の待ち（1 秒 × 再試行 5）のあとエラーになる
  - 途中で応答が止まった WALK は、そこまでの行 + 途中までの理由になる
  - WALK の失敗の理由（最初の段と途中の、タイムアウト・誤った v3 認証情報・
    知らない v3 ユーザ・機器が返した genErr）が 5.1.0 と同じ文言であること。
    7.1.28 の walk_cmd はエラーの応答に、直前に問い合わせた varBind をそのまま
    付けて返す。OID が増えないことの検出がエラーの応答まで比べると、どの
    失敗も「OID not increasing」に化ける（期待値は 5.1.0 の実測）
  - 取り消し: 次の応答を受けたところで効き、cancelled にそこまでの行が届く
  - 誤ったコミュニティ・誤った v3 パスワード・知らない v3 ユーザは失敗になる
  - GET/WALK ごとに作るイベントループとソケットが、終わったとき・取り消した
    とき・失敗したとき・例外のときに閉じ、スレッドも残らず、メインスレッドの
    ループを置き換えないこと
  - pysnmp の非推奨の名前を使っていないこと（実際に動かして警告を見る）

5.1.0 との違い（意図したもの。pysnmp 7.1.28 の walk_cmd の振る舞い）:
v1 の機器は、WALK の次が無いところ（MIB の終わりや、規格外の機器が返す
noSuchObject / noSuchInstance を v1 の応答側が変換したもの）を noSuchName
で返す。5.1.0 はこのとき直前の要求の行をもう一度返していたので、そこまで
WALK すると最後の行が 2 回載り、何も無いところからの WALK では要求した
OID が Null の 1 行になっていた（実測）。7.1.28 はそれぞれ 1 回・0 行に
なる。ここでは後者を期待する。それ以外の問い合わせ（このファイルの
GET/WALK と、誤った認証情報）は、5.1.0 と 7.1.28 で結果がすべて同じだった
（実測）。
OID が増えない応答は、部分木の外の前へ戻るものも含めて 5.1.0 と同じ
「OID not increasing」の途中までになる。WALK の応答の noSuchObject /
noSuchInstance（規格外の機器）は、5.1.0 と同じくそこで黙って終え、素の
NULL は 5.1.0 と同じく比べたあとで終える（snmp_manager._snmp_walk の
docstring）。

v1 の機器が v2 へ変換できない値を返す 4 件（LoopbackUnconvertibleV1ValueTest
と LoopbackResourceTest.test_an_unconvertible_value_releases_everything）は、
子プロセスで動かす（tests/snmp_isolated_scenarios.py。理由は
LoopbackUnconvertibleV1ValueTest の docstring）。

通信は 127.0.0.1 だけで、外部へは送らない。
"""
import asyncio
import os
import sys
import threading
import time
import unittest
import warnings
from unittest import mock

sys.path.insert(0, "src")
# 補助モジュール（tests/snmp_loopback_agent.py）を、起動の仕方によらず読めるように
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import snmp_loopback_agent as loopback   # noqa: E402
import snmp_isolated_scenarios as isolated   # noqa: E402
from pysnmp.proto import rfc1902, rfc1905   # noqa: E402
from pysnmp.smi import error as smi_error   # noqa: E402

HOST = loopback.HOST
ENTERPRISE = loopback.oid_text(loopback.ENTERPRISE)
# 応答の OID は MIB の名前で読んだ形になる（5.1.0 と同じ）
SHOWN = "SNMPv2-SMI::enterprises.32473"

# (OID の末尾, v1 の型名, v2c/v3 の型名, 値の文字列)
# v1 の INTEGER は Integer32、v2c/v3 は Integer として読まれる（5.1.0 でも同じ）
TYPED = (
    ("1.1.0", "OctetString", "OctetString", "netbelt-agent"),
    ("1.2.0", "OctetString", "OctetString",
     "0xe3838de38383e38388e38399e383abe38388"),
    ("1.3.0", "OctetString", "OctetString", "0x00005e005301"),
    ("1.4.0", "Integer32", "Integer", "-42"),
    ("1.5.0", "Counter32", "Counter32", "4000000000"),
    ("1.6.0", "Gauge32", "Gauge32", "1000000"),
    ("1.7.0", "TimeTicks", "TimeTicks", "8640000"),
    ("1.8.0", "IpAddress", "IpAddress", "192.0.2.1"),
    ("1.9.0", "ObjectIdentifier", "ObjectIdentifier", "1.3.6.1.4.1.32473.99.1"),
    # v1 では運べない（応答側が GET は noSuchName、GETNEXT は読み飛ばす）
    ("1.10.0", None, "Counter64", "18446744073709551615"),
)
LAST = (
    ("4.1.0", "Integer32", "Integer", "1"),
    ("4.2.0", "OctetString", "OctetString", "last"),
)

V3_USERS = {
    "noAuthNoPriv": loopback.v3_user("nb-none"),
    "authNoPriv": loopback.v3_user("nb-sha256", "SHA-256"),
    "authPriv": loopback.v3_user("nb-sha256-aes128", "SHA-256", "AES-128"),
}
# (名前, SNMPManager へ渡す引数)
PROFILES = (
    ("v1", {"version": "v1", "community": "public"}),
    ("v2c", {"version": "v2c", "community": "public"}),
) + tuple(("v3 " + level, loopback.netbelt_v3_params(user))
          for level, user in V3_USERS.items())


def expected(rows, version):
    """期待する行 [(OID, 型名, 値), ...]。v1 で運べない値は落とす"""
    result = []
    for suffix, v1_type, v2_type, value in rows:
        type_name = v1_type if version == "v1" else v2_type
        if type_name is not None:
            result.append(("%s.%s" % (SHOWN, suffix), type_name, value))
    return result


def request_oids(rows, version):
    """rows のうち、その版で問い合わせる OID（数字の形）"""
    return ["%s.%s" % (ENTERPRISE, suffix)
            for suffix, v1_type, _v2_type, _value in rows
            if version != "v1" or v1_type is not None]


def large_rows():
    return [("%s.3.%d.0" % (SHOWN, index), "OctetString", "row-%d" % index)
            for index in range(1, loopback.LARGE_ROWS + 1)]


SYSTEM = (1, 3, 6, 1, 2, 1, 1)
# 既定で読む MIB（SNMPv2-MIB）のオブジェクトへ、MIB と違う型で返す値。
# (OID, 相手役が返す値, 5.1.0 の NetBelt が出した行)。sysDescr.0・
# sysObjectID.0・sysLocation.0 は正しい型（比べる基準。OID の値は pysnmp が
# ObjectIdentity に置き換える）
MISMATCHED = (
    (SYSTEM + (1, 0), rfc1902.OctetString(b"netbelt-descr"),
     ("SNMPv2-MIB::sysDescr.0", "DisplayString", "netbelt-descr")),
    (SYSTEM + (2, 0), rfc1902.ObjectIdentifier(loopback.ENTERPRISE + (1,)),
     ("SNMPv2-MIB::sysObjectID.0", "ObjectIdentity",
      "SNMPv2-SMI::enterprises.32473.1")),
    (SYSTEM + (3, 0), rfc1902.OctetString(b"123"),
     ("SNMPv2-MIB::sysUpTime.0", "TimeTicks", "123")),
    (SYSTEM + (4, 0), rfc1902.Gauge32(5),
     ("SNMPv2-MIB::sysContact.0", "DisplayString", "5")),
    (SYSTEM + (5, 0), rfc1902.Integer32(-1),
     ("SNMPv2-MIB::sysName.0", "DisplayString", "-1")),
    (SYSTEM + (6, 0), rfc1902.OctetString(b"lab"),
     ("SNMPv2-MIB::sysLocation.0", "DisplayString", "lab")),
    (SYSTEM + (9, 1, 3, 1), rfc1902.TimeTicks(9),
     ("SNMPv2-MIB::sysORDescr.1", "DisplayString", "9")),
)


class _NonIncreasingAgent(loopback.LoopbackAgent):
    """GETNEXT に OID が増えない応答を返す相手役（壊れた機器の代わり）。

    LARGE_SUBTREE の 3 行目（32473.3.3.0）か、それより後ろの次を聞かれたら、
    answer の OID とその値を返す。answer が 3 行目なら同じ OID を返し続ける
    機器、1 行目なら前へ戻る機器になる。
    """

    def __init__(self, answer, **kwargs):
        super().__init__(**kwargs)
        self.answer = answer

    def _read_next(self, name):
        oid = tuple(name)
        subtree = loopback.LARGE_SUBTREE
        if oid[:len(subtree)] == subtree and oid >= subtree + (3, 0):
            return rfc1902.ObjectName(self.answer), self.values[self.answer]
        return super()._read_next(name)


class _GenErrAgent(loopback.LoopbackAgent):
    """GETNEXT に genErr（errorStatus 5）を返す相手役（中で失敗する機器の代わり）。

    LARGE_SUBTREE の 3 行目（32473.3.3.0）か、それより後ろの次を聞かれたら、
    計装が GenError を投げる。pysnmp の応答側はこれを genErr の応答にする
    （7.1.28 の pysnmp/entity/rfc3413/cmdrsp.py の process_pdu）。v1 でも genErr。
    """

    def _read_next(self, name):
        oid = tuple(name)
        subtree = loopback.LARGE_SUBTREE
        if oid[:len(subtree)] == subtree and oid >= subtree + (3, 0):
            raise smi_error.GenError(name=name, idx=0)
        return super()._read_next(name)


class _LoopbackTestCase(unittest.TestCase):
    """応答側と SNMPManager を用意する共通部分"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        # 作った SNMPManager はクラス終了まで保持する（キューに残った
        # シグナルの配送先を先に捨てると落ちる。既存のテストと同じ理由）
        cls._managers = []

    @classmethod
    def tearDownClass(cls):
        for _ in range(3):
            cls.app.processEvents()
        cls._managers.clear()

    def _agent(self, **kwargs):
        kwargs.setdefault("v3_users", list(V3_USERS.values()))
        agent = loopback.LoopbackAgent(**kwargs)
        agent.start()
        self.addCleanup(agent.stop)
        return agent

    def _recorder(self):
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        type(self)._managers.append(manager)
        return loopback.ManagerRecorder(manager)

    def _get(self, recorder, port, oids, params):
        return recorder.run(lambda: recorder.manager.snmp_get(
            HOST, list(oids), port=port, **params))

    def _walk(self, recorder, port, oid, params):
        return recorder.run(lambda: recorder.manager.snmp_walk(
            HOST, oid, port=port, **params))

    def assertSucceeded(self, recorder, rows):
        ok, payload = recorder.result()
        self.assertTrue(ok, "成功していない: %r" % (payload,))
        self.assertEqual(payload, rows)
        self.assertEqual(recorder.partial, [])
        self.assertEqual(recorder.cancelled, [])
        self.assertEqual(recorder.errors, [])

    def assertFailed(self, recorder):
        """失敗として届いたことを確かめ、エラーの文字列を返す"""
        ok, payload = recorder.result()
        self.assertFalse(ok, "失敗のはずが成功として届いた: %r" % (payload,))
        self.assertIsInstance(payload, str)
        self.assertTrue(payload.strip(), "エラーの理由が空")
        self.assertEqual(recorder.partial, [])
        self.assertEqual(recorder.cancelled, [])
        return payload

    def _run_in_child(self, name):
        """tests/snmp_isolated_scenarios.py のシナリオ name を子プロセスで動かし、子が返した記録を返す。

        時間切れ（子を止める）・子の中の失敗・終了コードは run_in_child が
        失敗にする。子が別の src（インストール済みの物など）を読んでいたら
        確かめが空振りするので、ここで同じファイルであることも確かめる。
        """
        import core.snmp_manager as snmp_manager
        report = isolated.run_in_child(name)
        self.assertTrue(
            os.path.samefile(report["snmp_manager"], snmp_manager.__file__),
            "子プロセスが別の snmp_manager を読んだ: %s" % report["snmp_manager"])
        return report


class LoopbackGetTest(_LoopbackTestCase):

    def test_get_returns_the_same_strings_as_5_1_0(self):
        """型ごとの値を 1 回の GET（複数 OID）で取り、OID・型名・値の文字列を比べる。"""
        agent = self._agent()
        recorder = self._recorder()
        for name, params in PROFILES:
            version = params["version"]
            with self.subTest(profile=name):
                self._get(recorder, agent.port, request_oids(TYPED, version),
                          params)
                self.assertSucceeded(recorder, expected(TYPED, version))

    def test_get_of_a_single_oid(self):
        agent = self._agent()
        recorder = self._recorder()
        self._get(recorder, agent.port, request_oids(TYPED[:1], "v2c"),
                  PROFILES[1][1])
        self.assertSucceeded(recorder, expected(TYPED[:1], "v2c"))

    def test_missing_oids_come_back_as_rows_in_v2c_and_v3(self):
        """noSuchInstance / noSuchObject は失敗ではなく、その行として返る。"""
        agent = self._agent()
        recorder = self._recorder()
        oids = [ENTERPRISE + ".1.1.0",
                ENTERPRISE + ".1.1.1",      # スカラはあるがインスタンスが無い
                ENTERPRISE + ".77.0"]       # 何も無い
        rows = [
            (SHOWN + ".1.1.0", "OctetString", "netbelt-agent"),
            (SHOWN + ".1.1.1", "NoSuchInstance",
             "No Such Instance currently exists at this OID"),
            (SHOWN + ".77.0", "NoSuchObject",
             "No Such Object currently exists at this OID"),
        ]
        for name, params in PROFILES[1:3]:
            with self.subTest(profile=name):
                self._get(recorder, agent.port, oids, params)
                self.assertSucceeded(recorder, rows)

    def test_a_missing_oid_is_a_no_such_name_error_in_v1(self):
        agent = self._agent()
        recorder = self._recorder()
        self._get(recorder, agent.port, [ENTERPRISE + ".1.1.1"], PROFILES[0][1])
        message = self.assertFailed(recorder)
        self.assertIn("noSuchName", message)


class LoopbackWalkTest(_LoopbackTestCase):

    def test_walk_stops_at_the_end_of_the_subtree(self):
        """部分木のすぐ外の隣の値（32473.2.0）を拾わない（5.1.0 の lexicographicMode=False と同じ）。"""
        agent = self._agent()
        recorder = self._recorder()
        for name, params in PROFILES:
            version = params["version"]
            with self.subTest(profile=name):
                self._walk(recorder, agent.port, ENTERPRISE + ".1", params)
                self.assertSucceeded(recorder, expected(TYPED, version))

    def test_walk_stops_at_the_end_of_the_mib_view(self):
        """MIB の最後の部分木。最後の行を 1 回だけ返し、途中までにしない。"""
        agent = self._agent()
        recorder = self._recorder()
        for name, params in PROFILES:
            version = params["version"]
            with self.subTest(profile=name):
                self._walk(recorder, agent.port, ENTERPRISE + ".4", params)
                self.assertSucceeded(recorder, expected(LAST, version))

    def test_a_walk_past_the_end_of_the_mib_view_is_an_empty_success(self):
        """最後の値より後ろからの WALK は、0 行の成功（v1 も。5.1.0 は Null の 1 行）。"""
        agent = self._agent()
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk(recorder, agent.port, ENTERPRISE + ".5", params)
                self.assertSucceeded(recorder, [])

    def test_a_long_walk_reports_progress_every_100_rows(self):
        agent = self._agent()
        recorder = self._recorder()
        self._walk(recorder, agent.port, ENTERPRISE + ".3", PROFILES[1][1])
        self.assertSucceeded(recorder, large_rows())
        self.assertIn("100件取得中...", recorder.progress)
        self.assertIn("200件取得中...", recorder.progress)


class LoopbackNonIncreasingOidTest(_LoopbackTestCase):
    """OID が増えない応答で、WALK が回り続けずに 5.1.0 と同じ結果で終わること。

    5.1.0 の GETNEXT は、応答の OID が問い合わせた OID 以下なら
    「OID not increasing」で WALK を終えていた。7.1.28 はこれを検出しない
    ので、NetBelt が見ないと同じ行を際限なく集め続ける（直す前の実測で
    1 秒に約 1000 行。止めるまで終わらない）。期待値は、5.1.0 の NetBelt
    （d2b37cc）で同じ相手役に問い合わせた実測値。
    """

    def _misbehaving_agent(self, answer):
        agent = _NonIncreasingAgent(answer, v3_users=list(V3_USERS.values()))
        agent.start()
        self.addCleanup(agent.stop)
        return agent

    def _walk_until_done(self, recorder, port, oid, params):
        # 5.1.0 は 0.1 秒ほどで終わる。回り続けたら、この待ちの上限で失敗する
        return recorder.run(lambda: recorder.manager.snmp_walk(
            HOST, oid, port=port, **params), timeout=30)

    def assertEndedAsPartial(self, recorder, rows):
        ok, payload = recorder.result()
        self.assertTrue(ok, "取れた行が捨てられた: %r" % (payload,))
        self.assertEqual(payload, rows)
        self.assertEqual(recorder.partial, ["OID not increasing"])
        self.assertEqual(recorder.cancelled, [])
        self.assertEqual(recorder.errors, [])

    def test_an_agent_that_repeats_the_same_oid(self):
        agent = self._misbehaving_agent(loopback.LARGE_SUBTREE + (3, 0))
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk_until_done(recorder, agent.port, ENTERPRISE + ".3",
                                      params)
                self.assertEndedAsPartial(recorder, large_rows()[:3])

    def test_an_agent_that_goes_back_inside_the_subtree(self):
        agent = self._misbehaving_agent(loopback.LARGE_SUBTREE + (1, 0))
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk_until_done(recorder, agent.port, ENTERPRISE + ".3",
                                      params)
                self.assertEndedAsPartial(recorder, large_rows()[:3])

    def test_an_agent_that_goes_back_outside_the_subtree(self):
        """部分木の外の前（32473.2.0）へ戻る機器も、5.1.0 と同じ途中まで。

        部分木の外へ出たことだけを見て終えると、黙って「全部取れた」ことに
        なり、部分木の残りの行を取りこぼしたことが利用者に伝わらない。
        """
        agent = self._misbehaving_agent(loopback.OUTSIDE_OID)
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk_until_done(recorder, agent.port, ENTERPRISE + ".3",
                                      params)
                self.assertEndedAsPartial(recorder, large_rows()[:3])

    def test_a_first_answer_equal_to_the_start_oid_is_an_error(self):
        """最初の応答が開始 OID と同じなら、行が無いのでエラー（5.1.0 と同じ文言）。"""
        agent = self._misbehaving_agent(loopback.LARGE_SUBTREE + (3, 0))
        recorder = self._recorder()
        self._walk_until_done(recorder, agent.port, ENTERPRISE + ".3.3.0",
                              PROFILES[1][1])
        self.assertEqual(self.assertFailed(recorder),
                         "SNMP Error: OID not increasing")


class _NoSuchValueAgent(loopback.LoopbackAgent):
    """GETNEXT に noSuchInstance / noSuchObject を返す相手役（規格外の機器の代わり）。

    RFC 3416 では GETNEXT の応答にこの 2 つは入らないが、返す機器はある。
    LARGE_SUBTREE の 3 行目（32473.3.3.0）か、それより後ろの次を聞かれたら、
    answer の OID を value で返す。v1 では応答側がこれを noSuchName にする。
    """

    def __init__(self, answer, value, **kwargs):
        super().__init__(**kwargs)
        self.answer = answer
        self.value = value

    def _read_next(self, name):
        oid = tuple(name)
        subtree = loopback.LARGE_SUBTREE
        if oid[:len(subtree)] == subtree and oid >= subtree + (3, 0):
            return rfc1902.ObjectName(self.answer), self.value
        return super()._read_next(name)


class LoopbackNoSuchValueWalkTest(_LoopbackTestCase):
    """WALK の応答に noSuchInstance / noSuchObject が来たら、そこで黙って終えること。

    5.1.0 の nextCmd は、pyasn1 の Null の仲間（noSuchObject・noSuchInstance・
    endOfMibView）をすべて WALK の終わりとして扱い、OID が増えないことの
    比較にも使わなかった（v2c で実測: 3 つの場合とも、そこまでの 3 行の成功）。
    7.1.28 の walk_cmd が終わりとして扱うのは pysnmp の rfc1902.Null と
    endOfMibView の値だけで、noSuchObject・noSuchInstance は行として渡して
    くる。そのままでは、この値の行が表に載ったり、「OID not increasing」の
    途中までになったりする（実測）。
    v1 では応答側が noSuchName にするので、v1 の MIB の終わりと同じく 3 行に
    なる（5.1.0 は最後の行をもう一度載せて 4 行。このファイルの冒頭の
    「5.1.0 との違い」）。期待値は 5.1.0 の v2c の実測。
    """

    def _no_such_agent(self, answer, value):
        agent = _NoSuchValueAgent(answer, value,
                                  v3_users=list(V3_USERS.values()))
        agent.start()
        self.addCleanup(agent.stop)
        return agent

    def _walk_until_done(self, recorder, port, params):
        # 5.1.0 は 0.1 秒ほどで終わる。回り続けたら、この待ちの上限で失敗する
        return recorder.run(lambda: recorder.manager.snmp_walk(
            HOST, ENTERPRISE + ".3", port=port, **params), timeout=30)

    def _assert_quiet_end(self, answer, value):
        agent = self._no_such_agent(answer, value)
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk_until_done(recorder, agent.port, params)
                self.assertSucceeded(recorder, large_rows()[:3])

    def test_a_no_such_instance_inside_the_subtree_ends_the_walk(self):
        self._assert_quiet_end(loopback.LARGE_SUBTREE + (4, 0),
                               rfc1905.NoSuchInstance(""))

    def test_a_no_such_object_outside_the_subtree_ends_the_walk(self):
        self._assert_quiet_end(loopback.OUTSIDE_OID, rfc1905.NoSuchObject(""))

    def test_a_repeated_oid_with_no_such_instance_ends_the_walk(self):
        """同じ OID を noSuchInstance で返し続けても、回り続けない。"""
        self._assert_quiet_end(loopback.LARGE_SUBTREE + (3, 0),
                               rfc1905.NoSuchInstance(""))


class LoopbackNullValueWalkTest(_LoopbackTestCase):
    """WALK の応答の値が素の NULL のとき、5.1.0 と同じ順で判定すること。

    5.1.0 は、OID が増えないことの比較から外すのが noSuchObject・
    noSuchInstance・endOfMibView の 3 つだけで、素の NULL は比べた
    （entity/rfc3413/cmdgen.py）。比べて増えていれば、そこで黙って終えた
    （hlapi の nextCmd）。NULL を比べる前に終えると、同じ OID や部分木の外の
    前の OID を NULL で返す機器で、取りこぼしが利用者に伝わらない。
    期待値は 5.1.0 の NetBelt（d2b37cc）の実測（v1・v2c・v3 とも同じ）。
    """

    def _null_agent(self, answer):
        agent = _NoSuchValueAgent(answer, rfc1902.Null(""),
                                  v3_users=list(V3_USERS.values()))
        agent.start()
        self.addCleanup(agent.stop)
        return agent

    def _walk_until_done(self, recorder, port, oid, params):
        return recorder.run(lambda: recorder.manager.snmp_walk(
            HOST, oid, port=port, **params), timeout=30)

    def test_a_null_for_the_same_oid_is_not_increasing(self):
        agent = self._null_agent(loopback.LARGE_SUBTREE + (3, 0))
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk_until_done(recorder, agent.port, ENTERPRISE + ".3",
                                      params)
                ok, rows = recorder.result()
                self.assertTrue(ok, "取れた行が捨てられた: %r" % (rows,))
                self.assertEqual(rows, large_rows()[:3])
                self.assertEqual(recorder.partial, ["OID not increasing"])
                self.assertEqual(recorder.cancelled, [])
                self.assertEqual(recorder.errors, [])

    def test_a_null_going_back_outside_the_subtree_is_not_increasing(self):
        agent = self._null_agent(loopback.OUTSIDE_OID)
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk_until_done(recorder, agent.port, ENTERPRISE + ".3",
                                      params)
                ok, rows = recorder.result()
                self.assertTrue(ok, "取れた行が捨てられた: %r" % (rows,))
                self.assertEqual(rows, large_rows()[:3])
                self.assertEqual(recorder.partial, ["OID not increasing"])
                self.assertEqual(recorder.cancelled, [])
                self.assertEqual(recorder.errors, [])

    def test_a_first_null_for_the_start_oid_is_an_error(self):
        """最初の応答が開始 OID と同じ OID の NULL なら、行が無いのでエラー。"""
        agent = self._null_agent(loopback.LARGE_SUBTREE + (3, 0))
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk_until_done(recorder, agent.port,
                                      ENTERPRISE + ".3.3.0", params)
                self.assertEqual(self.assertFailed(recorder),
                                 "SNMP Error: OID not increasing")

    def test_an_increasing_null_ends_the_walk_quietly(self):
        agent = self._null_agent(loopback.LARGE_SUBTREE + (4, 0))
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk_until_done(recorder, agent.port, ENTERPRISE + ".3",
                                      params)
                self.assertSucceeded(recorder, large_rows()[:3])


class LoopbackSymbolicStartWalkTest(_LoopbackTestCase):
    """開始 OID を記号名で書いた WALK が、数字で書いたときと同じ行になること。

    部分木の判定は、開始 OID をエンジンの MIB で解決した数字の組で行う。
    文字列を「.」で区切って数字にするような作りでは、記号名で壊れる。
    """

    def test_a_walk_from_a_symbolic_start_oid(self):
        agent = self._agent()
        recorder = self._recorder()
        for name, params in PROFILES:
            version = params["version"]
            with self.subTest(profile=name):
                self._walk(recorder, agent.port, "iso.3.6.1.4.1.32473.1", params)
                self.assertSucceeded(recorder, expected(TYPED, version))


class LoopbackGenErrTest(_LoopbackTestCase):
    """機器が WALK の途中・最初の段で genErr を返したとき、理由が 5.1.0 と同じ「genErr」であること。

    7.1.28 の walk_cmd は errorStatus の応答に、直前に問い合わせた varBind を
    そのまま付けて返す（pysnmp/hlapi/v3arch/asyncio/cmdgen.py 764 行目）。
    OID が増えないことの検出がこの応答まで比べると、genErr が「OID not
    increasing」に化ける。期待値は、5.1.0 の NetBelt（d2b37cc）で同じ相手役に
    問い合わせた実測値（v1・v2c・v3 とも同じ）。
    """

    def _generr_agent(self):
        agent = _GenErrAgent(v3_users=list(V3_USERS.values()))
        agent.start()
        self.addCleanup(agent.stop)
        return agent

    def test_a_gen_err_in_the_middle_keeps_the_rows_so_far(self):
        agent = self._generr_agent()
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk(recorder, agent.port, ENTERPRISE + ".3", params)
                ok, rows = recorder.result()
                self.assertTrue(ok, "取れた行が捨てられた: %r" % (rows,))
                self.assertEqual(rows, large_rows()[:3])
                self.assertEqual(recorder.partial, ["genErr"])
                self.assertEqual(recorder.cancelled, [])
                self.assertEqual(recorder.errors, [])

    def test_a_gen_err_on_the_first_step_is_an_error(self):
        agent = self._generr_agent()
        recorder = self._recorder()
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk(recorder, agent.port, ENTERPRISE + ".3.3.0", params)
                self.assertEqual(self.assertFailed(recorder),
                                 "SNMP Error: genErr")


class LoopbackMibTypeMismatchTest(_LoopbackTestCase):
    """既定で読む MIB のオブジェクトを機器が MIB と違う型で返しても、5.1.0 と同じに表示すること。

    pysnmp 7 は、その値を受信したタグと中身のまま MIB の型のクラスへ押し込み、
    その prettyPrint が中身を読めずに例外を投げる（sysContact.0 の Gauge32 は
    'Unsupported numeric type spec "255a" at DisplayString'）。直す前は GET
    全体が失敗し、WALK はその行で途切れていた。期待値（MISMATCHED）は、
    5.1.0 の NetBelt（d2b37cc）で同じ相手役に問い合わせた実測値（v1・v2c）。
    """

    def _mismatching_agent(self):
        return self._agent(values={oid: value for oid, value, _row in MISMATCHED})

    def test_get_shows_every_value_like_5_1_0(self):
        agent = self._mismatching_agent()
        recorder = self._recorder()
        oids = [loopback.oid_text(oid) for oid, _value, _row in MISMATCHED]
        rows = [row for _oid, _value, row in MISMATCHED]
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._get(recorder, agent.port, oids, params)
                self.assertSucceeded(recorder, rows)

    def test_walk_shows_every_row_like_5_1_0(self):
        agent = self._mismatching_agent()
        recorder = self._recorder()
        rows = [row for _oid, _value, row in MISMATCHED]
        for name, params in PROFILES:
            with self.subTest(profile=name):
                self._walk(recorder, agent.port, loopback.oid_text(SYSTEM),
                           params)
                self.assertSucceeded(recorder, rows)


class LoopbackInterruptedTest(_LoopbackTestCase):

    def test_rows_before_the_agent_stops_answering_are_kept(self):
        """途中で応答が止まった WALK は、そこまでの行と途中までの理由を返す。"""
        agent = self._agent()
        agent.drop_after(5)
        recorder = self._recorder()
        self._walk(recorder, agent.port, ENTERPRISE + ".3", PROFILES[1][1])

        ok, rows = recorder.result()
        served = agent.served
        self.assertTrue(ok, "取れた行が捨てられた: %r" % (rows,))
        self.assertGreaterEqual(served, 5)
        self.assertLess(served, loopback.LARGE_ROWS)
        self.assertEqual(rows, large_rows()[:served])
        self.assertEqual(len(recorder.partial), 1,
                         "途中までであることが知らされていない")
        # 5.1.0 と同じ文言（OID の比べ方を誤ると「OID not increasing」に化ける）
        self.assertEqual(recorder.partial[0],
                         "No SNMP response received before timeout")
        self.assertEqual(recorder.cancelled, [])
        self.assertEqual(recorder.errors, [])

    def test_a_cancelled_walk_hands_over_the_rows_so_far(self):
        """応答を待っている間に取り消すと、次の応答で止まり、そこまでの行が届く。"""
        agent = self._agent()
        agent.hold_after(3)
        recorder = self._recorder()
        recorder.start(lambda: recorder.manager.snmp_walk(
            HOST, ENTERPRISE + ".3", port=agent.port, **PROFILES[1][1]))
        self.assertTrue(loopback.pump_until(agent.held.is_set, 30),
                        "WALK が 3 行目の次の要求まで進まない")
        served = agent.served

        self.assertTrue(recorder.manager.request_cancel(),
                        "実行中の WALK の取り消しを受け付けない")
        agent.release()     # 次の応答が届く
        recorder.wait()

        self.assertEqual(recorder.cancelled, [large_rows()[:served]])
        self.assertEqual(recorder.completed, [],
                         "取り消したのに完了として届いた")
        self.assertEqual(recorder.partial, [])
        self.assertEqual(recorder.errors, [])

    def test_a_cancelled_get_is_not_reported_as_completed(self):
        agent = self._agent()
        agent.hold_after(0)
        recorder = self._recorder()
        recorder.start(lambda: recorder.manager.snmp_get(
            HOST, request_oids(TYPED[:1], "v2c"), port=agent.port,
            **PROFILES[1][1]))
        self.assertTrue(loopback.pump_until(agent.held.is_set, 30),
                        "GET の要求が届かない")

        self.assertTrue(recorder.manager.request_cancel())
        agent.release()
        recorder.wait()

        self.assertEqual(len(recorder.cancelled), 1,
                         "取り消しが知らされていない")
        # 5.1.0 は、取り消したあとに届いた GET の応答の行を渡していた。
        # 何も渡さなくてもよいが、別の行が混ざってはいけない
        for row in recorder.cancelled[0]:
            self.assertIn(row, expected(TYPED[:1], "v2c"))
        self.assertEqual(recorder.completed, [])
        self.assertEqual(recorder.errors, [])


class LoopbackUnconvertibleV1ValueTest(_LoopbackTestCase):
    """v1 の応答の値を pysnmp が v2 へ変換できないとき、GET/WALK がすぐ失敗で終わること。

    v1 の INTEGER は範囲の制限なしで復号されるが、pysnmp 7.1.28 は応答を
    要求と突き合わせて待ちの一覧から外したあとで、値を v2 の Integer32 へ
    変換する（pysnmp/proto/proxy/rfc2576.py 152 行目）。範囲を超える値
    （2147483648）だと、この変換の例外がイベントループのコールバックの中で
    止まり、待っている GET/WALK に届かない。待ちの一覧から外れているので
    再送もタイムアウトも働かず、取り消しも効かず、終わらなかった（実測）。
    5.1.0 では同じ応答で 0.5 秒ほどでエラーになっていた（実測）。

    この 3 件と LoopbackResourceTest.test_an_unconvertible_value_releases_everything
    は、子プロセスで動かす（相手役と GET/WALK は tests/snmp_isolated_scenarios.py
    のシナリオ）。同じプロセスで動かしていたときは、不具合が再発すると、
    制限時間で失敗はするが、止まったワーカー（QThread）・イベントループ・
    ソケットが pytest のプロセスに残った。取り消しも効かないので片付けられず、
    後続のテストへ影響したり、終了時に Qt が「QThread: Destroyed while thread
    is still running」で異常終了したりしうる。子プロセスなら、終わらなくても
    子ごと止めて、このテストだけの失敗にできる。判定は、子が返した記録
    （結果・かかった秒数・partial・cancelled）で、同じプロセスで動かして
    いたときと同じ条件で行う。
    """
    BIG = isolated.BIG

    def test_an_unconvertible_value_fails_the_get_at_once(self):
        report = self._run_in_child("an_unconvertible_value_fails_the_get_at_once")
        recorder = isolated.ReportedRecorder(report["recorder"])
        elapsed = report["elapsed"]
        message = self.assertFailed(recorder)
        self.assertIn("応答を処理できません", message)
        self.assertNotIn(str(self.BIG), message)
        # 既定の待ち（約 6 秒）を待たずに失敗する（5.1.0 と同じ）
        self.assertLess(elapsed, 4.0, "すぐに失敗しなかった（%.1f 秒）" % elapsed)

    def test_an_unconvertible_value_ends_the_walk_with_the_rows_so_far(self):
        report = self._run_in_child(
            "an_unconvertible_value_ends_the_walk_with_the_rows_so_far")
        recorder = isolated.ReportedRecorder(report["recorder"])
        ok, rows = recorder.result()
        self.assertTrue(ok, "取れた行が捨てられた: %r" % (rows,))
        self.assertEqual([row[2] for row in rows], ["1", "2"])
        self.assertEqual(len(recorder.partial), 1)
        self.assertIn("応答を処理できません", recorder.partial[0])
        self.assertEqual(recorder.cancelled, [])

    def test_an_unconvertible_first_value_fails_the_walk(self):
        report = self._run_in_child("an_unconvertible_first_value_fails_the_walk")
        recorder = isolated.ReportedRecorder(report["recorder"])
        elapsed = report["elapsed"]
        message = self.assertFailed(recorder)
        self.assertIn("応答を処理できません", message)
        self.assertLess(elapsed, 4.0, "すぐに失敗しなかった（%.1f 秒）" % elapsed)


class LoopbackFailureTest(_LoopbackTestCase):

    def test_an_unanswered_get_fails_after_the_default_wait(self):
        """応答しない相手には、既定の待ち（1 秒 × 再試行 5 ≒ 6 秒）のあと失敗する。

        下限だけを緩く見る（待ちを短くする変更を拾う）。上限は遅い
        ランナーのために見ない。
        """
        silent = loopback.SilentUdpPort()
        self.addCleanup(silent.close)
        recorder = self._recorder()
        elapsed = self._get(recorder, silent.port,
                            request_oids(TYPED[:1], "v2c"), PROFILES[1][1])
        self.assertFailed(recorder)
        self.assertGreaterEqual(elapsed, 4.0,
                                "既定の待ちより早く諦めた（%.1f 秒）" % elapsed)

    def test_a_wrong_v2c_community_is_not_a_success(self):
        agent = self._agent()
        recorder = self._recorder()
        self._get(recorder, agent.port, request_oids(TYPED[:1], "v2c"),
                  {"version": "v2c", "community": "netbelt-wrong"})
        self.assertFailed(recorder)

    def test_a_wrong_v3_auth_password_is_an_error(self):
        agent = self._agent()
        recorder = self._recorder()
        params = loopback.netbelt_v3_params(V3_USERS["authNoPriv"])
        params["auth_password"] = "netbelt-wrong-auth"
        self._get(recorder, agent.port, request_oids(TYPED[:1], "v2c"), params)
        message = self.assertFailed(recorder)
        self.assertIn("Wrong SNMP PDU digest", message)

    def test_a_wrong_v3_priv_password_is_an_error(self):
        """正しい組で通ることを先に確かめてから、暗号のパスワードだけを違える。"""
        agent = self._agent()
        recorder = self._recorder()
        params = loopback.netbelt_v3_params(V3_USERS["authPriv"])
        self._get(recorder, agent.port, request_oids(TYPED[:1], "v2c"), params)
        self.assertSucceeded(recorder, expected(TYPED[:1], "v2c"))

        params["priv_password"] = "netbelt-wrong-priv"
        self._get(recorder, agent.port, request_oids(TYPED[:1], "v2c"), params)
        self.assertFailed(recorder)

    def test_an_unknown_v3_user_is_an_error(self):
        agent = self._agent()
        recorder = self._recorder()
        params = loopback.netbelt_v3_params(loopback.v3_user("nb-nobody"))
        self._get(recorder, agent.port, request_oids(TYPED[:1], "v2c"), params)
        message = self.assertFailed(recorder)
        self.assertIn("Unknown USM user", message)

    def test_an_unanswered_walk_fails_with_the_timeout_reason(self):
        """最初の要求に応答が無い WALK は、既定の待ちのあと 5.1.0 と同じ文言で失敗する。

        WALK の最初の段の失敗は、行が無いのでエラーになる。OID の比べ方を
        誤ると、この文言が「OID not increasing」に化ける。
        """
        silent = loopback.SilentUdpPort()
        self.addCleanup(silent.close)
        recorder = self._recorder()
        elapsed = self._walk(recorder, silent.port, ENTERPRISE + ".3",
                             PROFILES[1][1])
        self.assertEqual(self.assertFailed(recorder),
                         "SNMP Error: No SNMP response received before timeout")
        self.assertGreaterEqual(elapsed, 4.0,
                                "既定の待ちより早く諦めた（%.1f 秒）" % elapsed)

    def test_a_walk_with_wrong_v3_credentials_fails_with_the_reason(self):
        """誤った v3 認証パスワード・知らない v3 ユーザの WALK は、5.1.0 と同じ文言で失敗する。"""
        agent = self._agent()
        recorder = self._recorder()
        wrong_auth = loopback.netbelt_v3_params(V3_USERS["authNoPriv"])
        wrong_auth["auth_password"] = "netbelt-wrong-auth"
        cases = (
            ("wrong auth password", wrong_auth,
             "SNMP Error: Wrong SNMP PDU digest"),
            ("unknown user",
             loopback.netbelt_v3_params(loopback.v3_user("nb-nobody")),
             "SNMP Error: Unknown USM user"),
        )
        for name, params, message in cases:
            with self.subTest(case=name):
                self._walk(recorder, agent.port, ENTERPRISE + ".3", params)
                self.assertEqual(self.assertFailed(recorder), message)


class LoopbackResourceTest(_LoopbackTestCase):
    """GET/WALK ごとのイベントループ・ソケット・スレッドが、どの終わり方でも残らないこと。

    snmp_manager は GET/WALK のたびに専用のループと SnmpEngine を作り、
    終わったとき・取り消したとき・失敗したとき・例外のときに閉じる。
    閉じ損ねると、ソケットとループが GC まで残り、名前解決のスレッド
    （asyncio の既定の executor）が操作のたびに増える。メインスレッドの
    ループを作ったり置き換えたりしないことも、目印のループで確かめる。
    """

    def setUp(self):
        import core.snmp_manager as snmp_manager
        self.spy = isolated.LoopSpy(snmp_manager._new_event_loop)
        patch = mock.patch.object(snmp_manager, "_new_event_loop", self.spy)
        patch.start()
        self.addCleanup(patch.stop)
        # メインスレッドのループを目印のループにしておく（置き換えられたら分かる）
        self.main_loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(self.main_loop)
        self.addCleanup(self.main_loop.close)
        self.addCleanup(asyncio.set_event_loop, None)

    def _agent(self, **kwargs):
        agent = super()._agent(**kwargs)
        # 相手役のスレッドとループ（テストの終わりまで動いている）は数えない
        self.threads_before = set(threading.enumerate())
        self.loops_before = isolated.open_loops()
        return agent

    def _assert_released(self, operations):
        """operations 回の GET/WALK が作ったものが、全部閉じていること"""
        self._assert_released_facts(
            isolated.released_facts(self.spy, self.main_loop,
                                    self.loops_before, self.threads_before),
            operations)

    def _assert_released_facts(self, facts, operations):
        """released_facts の材料で、operations 回の GET/WALK が作ったものが全部閉じていること。

        子プロセスで動かすテストも、子が返した材料をここで判定する。
        """
        self.assertEqual(facts["loops"], operations,
                         "GET/WALK ごとに 1 つのループではない")
        self.assertEqual(facts["sockets"], operations,
                         "GET/WALK ごとに 1 つのソケットではない（0 なら検査が空振り）")
        for running, closed in zip(facts["loops_running"], facts["loops_closed"]):
            self.assertFalse(running)
            self.assertTrue(closed, "GET/WALK のループが閉じていない")
        for fileno in facts["socket_filenos"]:
            self.assertEqual(fileno, -1, "GET/WALK の UDP のソケットが閉じていない")
        self.assertTrue(facts["main_loop_kept"], "メインスレッドのループが置き換わった")
        self.assertFalse(facts["main_loop_closed"])
        self.assertEqual(facts["leftover_loops"], [],
                         "閉じていないイベントループが残っている")
        self.assertEqual(facts["new_threads"], [], "スレッドが残っている")

    def test_completed_get_and_walk_release_everything(self):
        agent = self._agent()
        recorder = self._recorder()
        self._get(recorder, agent.port, request_oids(TYPED[:1], "v2c"),
                  PROFILES[1][1])
        self.assertSucceeded(recorder, expected(TYPED[:1], "v2c"))
        # v3 authPriv（鍵の導出と暗号化も通る）の WALK
        self._walk(recorder, agent.port, ENTERPRISE + ".4", PROFILES[4][1])
        self.assertSucceeded(recorder, expected(LAST, "v3"))
        self._assert_released(2)

    def test_an_unconvertible_value_releases_everything(self):
        """ループの例外で失敗させた GET/WALK（_LoopFailure の経路）も、全部閉じる。

        子プロセスで動かす（理由は LoopbackUnconvertibleV1ValueTest）。子は
        このクラスの setUp と同じ差し替え（LoopSpy・目印のループ）をして
        GET と WALK を行い、それぞれの記録と released_facts の材料を返す。
        """
        report = self._run_in_child("an_unconvertible_value_releases_everything")
        self.assertFailed(isolated.ReportedRecorder(report["get"]))
        recorder = isolated.ReportedRecorder(report["walk"])
        ok, rows = recorder.result()
        self.assertTrue(ok)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(recorder.partial), 1)
        self._assert_released_facts(report["released"], 2)

    def test_a_cancelled_walk_releases_everything(self):
        agent = self._agent()
        agent.hold_after(3)
        recorder = self._recorder()
        recorder.start(lambda: recorder.manager.snmp_walk(
            HOST, ENTERPRISE + ".3", port=agent.port, **PROFILES[1][1]))
        self.assertTrue(loopback.pump_until(agent.held.is_set, 30),
                        "WALK が 3 行目の次の要求まで進まない")
        self.assertTrue(recorder.manager.request_cancel())
        agent.release()
        recorder.wait()
        self.assertEqual(len(recorder.cancelled), 1, "取り消しが知らされていない")
        self._assert_released(1)

    def test_a_failed_get_releases_everything(self):
        agent = self._agent()
        recorder = self._recorder()
        params = loopback.netbelt_v3_params(loopback.v3_user("nb-nobody"))
        self._get(recorder, agent.port, request_oids(TYPED[:1], "v2c"), params)
        self.assertFailed(recorder)
        self._assert_released(1)

    def test_a_walk_that_raises_releases_everything(self):
        """WALK の途中で例外（経路が落ちた等）になっても、閉じてから行と理由を渡す。"""
        import core.snmp_manager as snmp_manager
        real_walk_cmd = snmp_manager.walk_cmd

        async def walk_cmd_that_raises(*args, **kwargs):
            # 本物の応答を 2 つ返したあと、反復そのものが例外を投げる
            count = 0
            async for response in real_walk_cmd(*args, **kwargs):
                yield response
                count += 1
                if count == 2:
                    raise OSError("netbelt-test: the route went away")

        patch = mock.patch.object(snmp_manager, "walk_cmd", walk_cmd_that_raises)
        patch.start()
        self.addCleanup(patch.stop)
        agent = self._agent()
        recorder = self._recorder()
        self._walk(recorder, agent.port, ENTERPRISE + ".3", PROFILES[1][1])

        ok, rows = recorder.result()
        self.assertTrue(ok, "取れた行が捨てられた: %r" % (rows,))
        self.assertEqual(rows, large_rows()[:2])
        self.assertEqual(recorder.partial,
                         ["OSError: netbelt-test: the route went away"])
        self._assert_released(1)


class LoopbackDeprecationTest(_LoopbackTestCase):

    def test_no_deprecated_pysnmp_names_are_used(self):
        """GET・WALK・Trap 受信を実際に動かし、DeprecationWarning が 1 つも出ないこと。

        pysnmp 7 は旧名（getCmd・addTransport・registerObserver・
        transportDispatcher など）を __getattr__ で新名へ転送し、
        DeprecationWarning を出すだけで動いてしまう。次の版で旧名が消えると
        SNMP が使えなくなるので、ここで拾う。警告はワーカーや受信の
        スレッドの中で出るので、プロセス全体の警告を記録する。
        """
        from conftest import trap_bytes
        import socket
        agent = self._agent()
        recorder = self._recorder()
        manager = recorder.manager
        traps = []
        manager.trap_received.connect(traps.append)
        port = loopback.free_udp_port()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self._get(recorder, agent.port, request_oids(TYPED, "v3"),
                      PROFILES[4][1])
            self.assertSucceeded(recorder, expected(TYPED, "v3"))
            self._walk(recorder, agent.port, ENTERPRISE + ".1", PROFILES[0][1])
            self.assertSucceeded(recorder, expected(TYPED, "v1"))

            self.assertTrue(manager.start_trap_receiver(port, ["public"]),
                            recorder.errors)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(trap_bytes("public"), (HOST, port))
            finally:
                sender.close()
            received = loopback.pump_until(lambda: traps, 15)
            manager.stop_trap_receiver()
        self.assertTrue(received, "Trap が届かない（検査が空振り）")
        # 見るのは pysnmp と NetBelt（src・tests）の中から出た警告だけ。
        # この間に初めて読まれた別のライブラリの警告（asyncore など）は数えない
        import pysnmp
        here = os.path.dirname(os.path.abspath(__file__))
        roots = tuple(os.path.normcase(os.path.abspath(path)) + os.sep for path in (
            os.path.dirname(pysnmp.__file__), os.path.join(here, "..", "src"), here))
        deprecated = ["%s:%s: %s" % (os.path.basename(w.filename), w.lineno,
                                     w.message)
                      for w in caught
                      if issubclass(w.category, DeprecationWarning)
                      and os.path.normcase(os.path.abspath(w.filename)).startswith(roots)]
        self.assertEqual(deprecated, [])


if __name__ == "__main__":
    unittest.main()
