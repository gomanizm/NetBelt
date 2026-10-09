"""
SNMPマネージャー

GET/WALK/Trap受信をサポートし、SNMPv1/v2c/v3に対応
"""
from PyQt6.QtCore import QObject, pyqtSignal, QThread
from typing import List, Dict, Optional, Tuple
import asyncio
import contextlib
import os
import socket
import threading
import traceback
from datetime import datetime
from .sockets import set_exclusive_bind

# pysnmp 7.x 用のインポート。7.x の API は asyncio だけで、同期の getCmd /
# nextCmd や asyncore のトランスポートは無い。pyasn1 との組み合わせによっては
# 読み込めない（5.1.0 は pyasn1 0.6.4 に無い pyasn1.compat.octets を使う）ので、
# 失敗しても本体は起動し、SNMP 機能だけが使えない状態にする。
try:
    from pysnmp.carrier.asyncio.dgram import udp
    from pysnmp.carrier.asyncio.dispatch import AsyncioDispatcher
    from pysnmp.entity import config
    from pysnmp.entity.engine import SnmpEngine
    from pysnmp.entity.rfc3413 import ntfrcv
    from pysnmp.hlapi.varbinds import CommandGeneratorVarBinds
    from pysnmp.proto import errind, rfc1902, rfc1905
    from pyasn1.type import univ
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData, ContextData, ObjectIdentity, ObjectType,
        UdpTransportTarget, UsmUserData, get_cmd, walk_cmd,
        USM_AUTH_NONE, USM_AUTH_HMAC96_MD5, USM_AUTH_HMAC96_SHA,
        USM_AUTH_HMAC128_SHA224, USM_AUTH_HMAC192_SHA256,
        USM_AUTH_HMAC256_SHA384, USM_AUTH_HMAC384_SHA512,
        USM_PRIV_NONE, USM_PRIV_CBC56_DES, USM_PRIV_CBC168_3DES,
        USM_PRIV_CFB128_AES, USM_PRIV_CFB192_AES, USM_PRIV_CFB256_AES)
    _PYSNMP_AVAILABLE = True
except Exception:
    _PYSNMP_AVAILABLE = False

try:
    from .mib_resolver import get_resolver
except Exception:
    get_resolver = None


# SNMPv3 (USM) で選べるプロトコルの名前。UI のコンボボックスと共有する。
V3_AUTH_PROTOCOL_NAMES = ("none", "MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512")
V3_PRIV_PROTOCOL_NAMES = ("none", "DES", "3DES", "AES-128", "AES-192", "AES-256")

# RFC 3414 が USM のパスワードに要求する最小長
V3_PASSWORD_MIN_LENGTH = 8

def _clean_communities(communities):
    """コミュニティ一覧から空文字と重複を落とす（順序は保つ）

    空文字を登録すると「コミュニティ無しの Trap を受け入れる」設定が
    意図せず作れてしまう。重複はそのぶん余計な行を pysnmp へ登録する。
    """
    # 文字列をそのまま渡されると1文字ずつのコミュニティになる。
    # 黙って通すと 'public' が p/u/b/l/i/c の6件として登録される。
    if isinstance(communities, (str, bytes)):
        raise TypeError("communities はコミュニティ名のリストです"
                        "（文字列1つではありません）")

    cleaned = []
    for community in communities:
        if not isinstance(community, str):
            continue
        community = community.strip()
        if community and community not in cleaned:
            cleaned.append(community)
    return cleaned


# pysnmp は v1 Trap を v1ToV2 変換に通すとき snmpTrapCommunity を合成し、
# コミュニティ文字列そのものを varbind として足す。v1/v2c ではこれが
# 唯一の認証情報で、CSV/JSON/TXT のエクスポートはチケットや報告書へ回る
# ため、値は表示にもファイルにも載せない。7.1.28 でも同じ OID で合成する
# （pysnmp/proto/proxy/rfc2576.py の v1_to_v2、135 行目）。
SNMP_TRAP_COMMUNITY_OID = '1.3.6.1.6.3.18.1.4.0'


def resolve_v3_protocols(auth_name: str, priv_name: str):
    """
    プロトコル名から pysnmp の USM 定数を引く

    未知の名前を黙って「認証なし」に落とすと、認証失敗の原因が追えなくなる。
    ここで例外にして表面化させる。

    Args:
        auth_name: V3_AUTH_PROTOCOL_NAMES のいずれか
        priv_name: V3_PRIV_PROTOCOL_NAMES のいずれか

    Returns:
        (認証プロトコル定数, 暗号プロトコル定数) のタプル

    Raises:
        ValueError: 未知の名前、または認証なしで暗号化を指定した場合
        RuntimeError: pysnmp が利用できない場合
    """
    if not _PYSNMP_AVAILABLE:
        raise RuntimeError("SNMPライブラリ(pysnmp)を利用できません")

    # SHA-2 系の定数名は「HMAC<出力ビット長>_SHA<ダイジェスト長>」の順であり、
    # SHA-256 は USM_AUTH_HMAC192_SHA256 になる（HMAC256 ではない）。
    # pysnmp 5.1.0 での名前は usmHMAC192SHA256AuthProtocol で、並びは同じ。
    auth_table = {
        "none": USM_AUTH_NONE,
        "MD5": USM_AUTH_HMAC96_MD5,
        "SHA": USM_AUTH_HMAC96_SHA,
        "SHA-224": USM_AUTH_HMAC128_SHA224,
        "SHA-256": USM_AUTH_HMAC192_SHA256,
        "SHA-384": USM_AUTH_HMAC256_SHA384,
        "SHA-512": USM_AUTH_HMAC384_SHA512,
    }
    # AES-192/256 は Reeder 版（名前に BLUMENTHAL が付かない方）を使う。
    # pysnmp のソースが「non-standard but used by many vendors」と書いている方で
    # （7.1.28 の pysnmp/entity/config.py 102〜103 行目）、Cisco 等の実装と
    # 相互接続するのはこちら。
    # 5.1.0 での名前は usmAesCfb192Protocol / usmAesCfb256Protocol。
    priv_table = {
        "none": USM_PRIV_NONE,
        "DES": USM_PRIV_CBC56_DES,
        "3DES": USM_PRIV_CBC168_3DES,
        "AES-128": USM_PRIV_CFB128_AES,
        "AES-192": USM_PRIV_CFB192_AES,
        "AES-256": USM_PRIV_CFB256_AES,
    }

    if auth_name not in auth_table:
        raise ValueError(
            f"未知の認証プロトコル: {auth_name}（選べるのは {', '.join(V3_AUTH_PROTOCOL_NAMES)}）")
    if priv_name not in priv_table:
        raise ValueError(
            f"未知の暗号プロトコル: {priv_name}（選べるのは {', '.join(V3_PRIV_PROTOCOL_NAMES)}）")
    if auth_name == "none" and priv_name != "none":
        raise ValueError(
            "認証なしでは暗号化を使えません（SNMPv3 では authNoPriv 以上が必要です）")

    return auth_table[auth_name], priv_table[priv_name]


def v3_password_error(auth_protocol: str, auth_password: str,
                      priv_protocol: str, priv_password: str):
    """
    v3 のパスワード長を調べ、問題があれば説明を返す（無ければ None）

    RFC 3414 は USM のパスワードに8文字以上を要求している。pysnmp も
    これに従うが、短いときのエラーが利用者向けでない。空文字は鍵導出の
    割り算で ZeroDivisionError、1〜7文字は WrongValueError になり、
    どちらもパスワードが原因だと読み取れない。手前で止める。
    """
    for label, protocol, password in (("認証", auth_protocol, auth_password),
                                      ("暗号", priv_protocol, priv_password)):
        if not protocol or protocol == "none":
            continue
        if len(password or "") < V3_PASSWORD_MIN_LENGTH:
            return (f"{label}パスワードは{V3_PASSWORD_MIN_LENGTH}文字以上にしてください"
                    "（SNMPv3 の要件です）。")
    return None


# 「途中まで（… のため中断）」の理由に使う長さの上限。pysnmp の
# dispatcher は poll error の本文へトレースバックを丸ごと入れるため
# （実測 2097 バイト）、そのまま画面のステータスへ出すと読めない。
PARTIAL_REASON_MAX_LENGTH = 120


def short_failure_reason(error: BaseException) -> str:
    """例外から、1 行の短い中断理由を作る

    str(e) は複数行のことがあるので先頭行だけを使い、情報の無い
    「Traceback (most recent call last):」は落とす。本文が空の例外
    （OSError() など）もあるので、型名を必ず前に付ける。
    """
    text = str(error)
    first_line = text.splitlines()[0].strip() if text else ""
    header = "Traceback (most recent call last):"
    if first_line.endswith(header):
        first_line = first_line[:-len(header)].strip()
    first_line = first_line.rstrip(":").strip()
    name = type(error).__name__
    if not first_line:
        return name
    if len(first_line) > PARTIAL_REASON_MAX_LENGTH:
        first_line = first_line[:PARTIAL_REASON_MAX_LENGTH] + "…"
    return f"{name}: {first_line}"


def _new_event_loop():
    """pysnmp の処理 1 回ぶん（GET 1 回・WALK 1 回・Trap 受信 1 回）の専用ループを作る

    Windows の既定の ProactorEventLoop ではなく SelectorEventLoop を使う。
    Proactor のデータグラム受信は、ポート到達不能以外の OSError を受けると
    error_received を呼んだあと次の受信を出し直さず、以後は黙って何も
    受け取らなくなる（Python 3.11 の asyncio/proactor_events.py の
    _ProactorDatagramTransport._loop_reading）。Selector は error_received の
    あとも読み続ける。5.1.0 の asyncore と同じ select() による待ち方でもある。
    例外ハンドラは _quiet_loop_exception に替える。
    """
    loop = asyncio.SelectorEventLoop()
    loop.set_exception_handler(_quiet_loop_exception)
    return loop


def _quiet_loop_exception(loop, context):
    """ループが拾った例外を、受信したデータを載せずに 1 行で出す

    asyncio の既定の例外ハンドラは、例外が出たコールバックの引数を表示する。
    pysnmp の受信コールバックの引数は、受信したパケットのバイト列と送信元
    （BER として読めないパケットで pysnmp の復号が例外を出し、先頭の
    バイト列がそのまま出た。実測）。v1/v2c のパケットにはコミュニティが
    平文で入っており、凍結ビルドでは stdout と stderr をログファイルへ
    恒久保存する（main.py の _setup_logging）。例外の文にも受信した値が
    入りうるので、型名と、例外が出た場所（ファイル名と行番号。値は含まない）
    だけを出す。受信は続く（ループは止まらない）。
    ここで例外を漏らすと、asyncio は既定のハンドラで元の context を
    まるごと出す（stdout が日本語を書けないときの print の失敗など）ので、
    すべて握る。
    """
    try:
        error = context.get("exception")
        name = type(error).__name__ if error is not None else "不明"
        where = ""
        frames = (traceback.extract_tb(error.__traceback__)
                  if error is not None else [])
        if frames:
            where = f"（{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}）"
        print(f"[SNMP] イベントループの処理で例外を捨てました: {name}{where}")
    except Exception:
        pass


def _close_event_loop(loop):
    """止まっているイベントループの残りを片付けてから閉じる

    asyncio.run() の後始末と同じ手順。取り消したタスク（ディスパッチャの
    タイマ）を終わらせ、transport.close() が積んだ後始末（ソケットを閉じる）と、
    名前解決に使ったスレッド（既定の executor）の終了をここで回してから閉じる。
    回さずに閉じると、ソケットとスレッドが GC まで残る。
    """
    try:
        tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            loop.run_until_complete(
                asyncio.gather(*tasks, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception as e:
        # 後始末の失敗で、呼び出し側の結果や本来の例外を上書きしない
        print(f"[SNMP] イベントループの後始末に失敗しました: {e}")
    finally:
        loop.close()


class _LoopFailure:
    """GET/WALK のループで拾った例外を、待っている操作の失敗にする

    pysnmp 7.1.28 の受信は、応答を要求と突き合わせて待ちの一覧から外した
    あとで、v1 の値を v2 の型へ変換する（pysnmp/proto/proxy/rfc2576.py
    152 行目）。範囲を超える INTEGER（2147483648 など）でこの変換が例外を
    出すと、例外はイベントループのコールバックの中で止まり、待っている
    get_cmd / walk_cmd に届かない。待ちの一覧から外れているので再送も
    タイムアウトも働かず、取り消しも効かずに終わらなくなる（実測）。
    5.1.0 は同じ応答ですぐエラーになっていた（実測）。ループの例外ハンドラで
    拾った例外を、待っている操作の失敗に変える。文言は型名だけにする
    （例外の文には受信した値が入る）。
    """

    def __init__(self, loop):
        self._loop = loop
        self._failed = loop.create_future()
        loop.set_exception_handler(self._handle)

    def _handle(self, loop, context):
        _quiet_loop_exception(loop, context)
        if not self._failed.done():
            error = context.get("exception")
            name = type(error).__name__ if error is not None else "不明"
            self._failed.set_exception(
                RuntimeError(f"SNMP の応答を処理できません（{name}）"))

    def run(self, coroutine):
        """coroutine を回して結果を返す。先にループで例外を拾ったら、その失敗を投げる"""
        task = self._loop.create_task(coroutine)
        self._loop.run_until_complete(asyncio.wait(
            {task, self._failed}, return_when=asyncio.FIRST_COMPLETED))
        if task.done():
            return task.result()
        task.cancel()
        self._loop.run_until_complete(
            asyncio.gather(task, return_exceptions=True))
        return self._failed.result()

    def close(self):
        """誰も受け取らなかった失敗を片付ける（ループを閉じる前に呼ぶ）"""
        if not self._failed.done():
            self._failed.cancel()
        elif not self._failed.cancelled():
            self._failed.exception()


def _close_dispatcher(engine):
    """エンジンのディスパッチャを閉じる（トランスポートを閉じ、タイマを止める）

    ディスパッチャが無ければ何もしない。閉じたトランスポートのソケットは、
    このあと _close_event_loop がループを回したところで実際に閉じる。
    """
    try:
        engine.close_dispatcher()
    except Exception as e:
        print(f"[SNMP] ディスパッチャを閉じられませんでした: {e}")


def _value_text(value):
    """GET/WALK で受け取った値を、表示する文字列にする

    pysnmp 7 は、既定で読む MIB に定義のあるオブジェクト（system グループ
    など）の値を、受信したタグと中身のまま MIB の型のクラスへ押し込む
    （7.1.28 の pysnmp/smi/rfc1902.py の ObjectType.resolve_with_mib、
    1063〜1071 行目）。機器が MIB と違う型で返すと（sysContact.0 を Gauge32 で
    返すなど）、MIB の型の prettyPrint が中身を読めずに SmiError を投げ、
    GET 全体が失敗し、WALK はその行で途切れる。5.1.0 は MIB の型へ値を
    変換して表示していた（Gauge32(5) の sysContact.0 は "5"。実測）。
    クラスと受信したタグが食い違う値だけ、受信したタグの基本型で表示する
    （実測した組み合わせでは 5.1.0 と同じ文字列になる）。中身は _value から
    取る（pysnmp が受信した値をそのまま入れた属性で、同じ関数の 1065・1071
    行目で pysnmp 自身もこの属性を読み書きする）。型名は呼び出し側が MIB の
    型のクラス名のまま出す。5.1.0 も変換できたときは MIB の型の名前だったが、
    変換できなかったとき（sysServices.0 に OctetString など）は受信した型の
    名前だったので、そこだけ型名が違う。
    クラスにタグが無い値はそのまま prettyPrint する。OID の値は pysnmp が
    ObjectIdentity（pyasn1 の型ではない）に置き換えて渡し（同じ関数の
    1089〜1094 行目）、_snmp_get / _snmp_walk を差し替えたテストは
    prettyPrint だけを持つ値を渡す。
    """
    tag_set = getattr(value, "tagSet", None)
    class_tag_set = getattr(type(value), "tagSet", None)
    received_type = _RECEIVED_TYPES.get(tag_set)
    if (received_type is None or class_tag_set is None
            or class_tag_set == tag_set):
        return value.prettyPrint()
    return received_type(value._value).prettyPrint()


# 受信したタグ → SNMP の基本型（_value_text が使う）。Unsigned32 は Gauge32 と、
# Bits は OctetString と同じタグ
_RECEIVED_TYPES = ({cls.tagSet: cls for cls in (
    rfc1902.Integer32, rfc1902.OctetString, rfc1902.IpAddress,
    rfc1902.Counter32, rfc1902.Gauge32, rfc1902.TimeTicks, rfc1902.Opaque,
    rfc1902.Counter64, rfc1902.ObjectIdentifier)}
    if _PYSNMP_AVAILABLE else {})


def _snmp_get(auth_data, host, port, oids):
    """SNMP GET を 1 回行い、(errorIndication, errorStatus, errorIndex, varBinds) を返す

    pysnmp 7 には同期の getCmd が無い。呼ぶたびに専用のイベントループと
    SnmpEngine を作り、返す前にどちらも閉じる。run_until_complete は、
    走っている間だけそのスレッドの「実行中のループ」になるだけなので、
    GUI スレッドや他のスレッドの既定ループを作らず、奪わない。
    待ち時間は UdpTransportTarget の既定（timeout 1 秒 × retries 5。7.1.28 の
    pysnmp/hlapi/transport.py 28〜29 行目）のまま。

    SNMPWorker はこの関数を通してだけ pysnmp の GET を呼ぶ。テストは
    これを差し替えて、機器なしでワーカーの判定を確かめる。
    """
    loop = _new_event_loop()
    failure = None
    engine = None
    try:
        # ループを作ったあとで作る（作るところで落ちても、ループを閉じる）
        failure = _LoopFailure(loop)
        engine = SnmpEngine()

        async def query():
            target = await UdpTransportTarget.create((host, port))
            return await get_cmd(
                engine, auth_data, target, ContextData(),
                *[ObjectType(ObjectIdentity(oid)) for oid in oids])

        return failure.run(query())
    finally:
        if engine is not None:
            _close_dispatcher(engine)
        if failure is not None:
            failure.close()
        _close_event_loop(loop)


def _snmp_walk(auth_data, host, port, oid):
    """SNMP WALK を行い、応答 1 つごとに 4 つ組を返すジェネレータ

    返す形は _snmp_get と同じ (errorIndication, errorStatus, errorIndex, varBinds)。
    部分木の外へ出たところで終わる（5.1.0 の nextCmd の lexicographicMode=False
    と同じ）。この判定は walk_cmd に任せず、下の「OID が増えない」の比較の
    あとで自分で行う（walk_cmd は lexicographicMode=True で回す）。

    5.1.0 との違い: v1 の機器は MIB の終わりを noSuchName のエラーで返す。
    7.1.28 の walk_cmd はこれを何も返さずに終わる（pysnmp/hlapi/v3arch/
    asyncio/cmdgen.py 758〜763 行目）。5.1.0 の nextCmd はエラーを消した
    うえで直前の要求の行をもう一度返していたので、最後の行が 2 回載り、
    何も無いところからの WALK では要求した OID が Null の 1 行になっていた
    （実測）。7.1.28 では、それぞれ 1 回・0 行になる。
    ループと SnmpEngine は最初の next() で作り、最後まで回ったとき・例外で
    抜けたとき・途中で close() されたとき（取り消し）に閉じる。途中で
    やめるときは、pysnmp の非同期ジェネレータを aclose してから閉じる。
    途中で抜ける呼び出し側は close() すること（SNMPWorker は
    contextlib.closing で閉じる）。

    OID が増えない応答（同じ OID を返す・前へ戻る機器）: 5.1.0 の nextCmd は、
    応答の OID が問い合わせた OID 以下なら errorIndication を「OID not
    increasing」にして WALK を終えていた（5.1.0 の pysnmp/entity/rfc3413/
    cmdgen.py の getNextVarBinds）。7.1.28 の NextCommandGenerator は
    この比較をしない（pysnmp/entity/rfc3413/cmdgen.py 432 行目からの
    process_response_varbinds。walk_cmd にあるのは、この errorIndication を
    無視するオプションだけで、検出はしない）。そのままでは同じ行を際限なく
    集め続ける（実測で 1 秒に約 1000 行）ので、ここで 5.1.0 と同じ比べ方を
    して errind.oidNotIncreasing を返して終える。ワーカーはこれを、ほかの
    errorIndication と同じく「行が無ければエラー、あれば途中まで」にする。
    比べる相手は直前に問い合わせた OID（最初は開始 OID）で、開始 OID は
    walk_cmd と同じ手順（エンジンの MIB で解決）で数字にしておく。
    値による終わりの判定は 5.1.0 と同じ順で行う。noSuchObject・
    noSuchInstance は比べずにそこで黙って終える（GETNEXT の応答にこの 2 つを
    入れるのは規格外（RFC 3416）だが、返す機器はある）。素の NULL は比べた
    うえで、増えていればそこで黙って終える。5.1.0 は比較から外すのが
    noSuchObject・noSuchInstance・endOfMibView の 3 つだけで（5.1.0 の
    entity/rfc3413/cmdgen.py）、pyasn1 の Null の仲間はすべて WALK の終わり
    にしていた（5.1.0 の hlapi の nextCmd）。7.1.28 の walk_cmd が自分で
    終えるのは pysnmp の rfc1902.Null と endOfMibView の値だけで
    （cmdgen.py 772 行目）、受信した NULL（pyasn1 の univ.Null）と
    noSuchObject・noSuchInstance は行として渡してくる。
    部分木の外の、前へ戻った応答も「OID not increasing」の途中までにする
    （5.1.0 と同じ）。walk_cmd に lexicographicMode=False で部分木の判定を
    任せると、この応答は OID を見せずにその場で終わり、そこまでの行の
    成功になる。部分木の残りを取りこぼしたことが利用者に伝わらない。

    SNMPWorker はこの関数を通してだけ pysnmp の WALK を呼ぶ。テストは
    これを差し替えて、機器なしでワーカーの判定を確かめる。
    """
    loop = _new_event_loop()
    failure = None
    engine = None
    responses = None
    try:
        # ループを作ったあとで作る（作るところで落ちても、ループを閉じる）
        failure = _LoopFailure(loop)
        engine = SnmpEngine()
        target = failure.run(UdpTransportTarget.create((host, port)))
        request = ObjectType(ObjectIdentity(oid))
        # walk_cmd の中と同じ呼び出しで解決する（解決済みの ObjectType は
        # walk_cmd がそのまま使う）。解決できない OID の例外もここで出る
        previous = CommandGeneratorVarBinds().make_varbinds(
            engine.cache, (request,))[0][0].get_oid().asTuple()
        subtree = previous
        responses = walk_cmd(engine, auth_data, target, ContextData(),
                             request, lexicographicMode=True)

        async def next_response():
            try:
                return await responses.__anext__()
            except StopAsyncIteration:
                return None

        while True:
            response = failure.run(next_response())
            if response is None:
                return
            errorIndication, errorStatus, _errorIndex, varBinds = response
            # エラーの応答では比べない。walk_cmd はエラーに、直前に問い合わせた
            # varBind をそのまま付けて返す（7.1.28 の cmdgen.py 755・764 行目）
            # ので、比べるとタイムアウトも genErr も「OID not increasing」に化ける
            if not errorIndication and not errorStatus and varBinds:
                value = varBinds[0][1]
                if isinstance(value, (rfc1905.NoSuchObject,
                                      rfc1905.NoSuchInstance)):
                    return
                found = varBinds[0][0].get_oid().asTuple()
                if found <= previous:
                    yield (errind.oidNotIncreasing, 0, 0, varBinds)
                    return
                if found[:len(subtree)] != subtree or isinstance(value, univ.Null):
                    return
                previous = found
            yield response
    finally:
        if responses is not None:
            try:
                loop.run_until_complete(responses.aclose())
            except Exception:
                pass
        if engine is not None:
            _close_dispatcher(engine)
        if failure is not None:
            failure.close()
        _close_event_loop(loop)


class SNMPWorker(QThread):
    """SNMP操作を別スレッドで実行するワーカー"""
    
    # シグナル定義
    result_ready = pyqtSignal(bool, object)  # (success, result)
    progress_update = pyqtSignal(str)  # ステータスメッセージ
    # WALK が途中で途切れたときの理由。取れた分は result_ready で普通に
    # 渡すので、不完全であることはこちらで伝える
    partial_result = pyqtSignal(str)
    # 取り消されて終わったときに、そこまでに取れた行を渡す。
    # result_ready とはどちらか一方だけが出る
    cancelled = pyqtSignal(object)

    def __init__(self, operation: str, params: dict):
        super().__init__()
        self.operation = operation
        self.params = params
        self._cancelled = False
        self._partial_reason = None
        # 取り消されたときに渡す、そこまでに取れた行（WALK が貯めていく）
        self._collected = []

    def run(self):
        """スレッドのメイン処理"""
        try:
            if not _PYSNMP_AVAILABLE:
                self.result_ready.emit(False, "SNMPライブラリ(pysnmp)を利用できません")
                return
            if self.operation == "get":
                result = self._perform_get()
            elif self.operation == "walk":
                result = self._perform_walk()
            else:
                self.result_ready.emit(False, f"不明な操作: {self.operation}")
                return
            
            if self._cancelled:
                # 利用者が止めた。取れた分は途中までとして渡す
                self.cancelled.emit(result)
            else:
                if self._partial_reason:
                    # 表には出すが、全部ではないことを先に伝える
                    self.partial_result.emit(self._partial_reason)
                self.result_ready.emit(True, result)

        except Exception as e:
            if self._cancelled:
                # 止めたあとの失敗（応答待ちのタイムアウト等）はエラーに
                # しない。そこまでの行（GET なら空）を途中までとして渡す
                self.cancelled.emit(list(self._collected))
            elif self._collected:
                # WALK の途中で例外（経路が落ちた、ソケットが閉じた）。
                # errorIndication のときと同じく、取れた行は捨てずに
                # 「途中まで」として渡す。ここで捨てると、直前までの
                # 行は表にも書き出しにも出ないまま消える（実測）。
                # 理由は 1 行に切り詰める（str(e) はトレースバックを
                # 丸ごと抱えていることがある）
                self.partial_result.emit(short_failure_reason(e))
                self.result_ready.emit(True, list(self._collected))
            else:
                self.result_ready.emit(False, str(e))

    def cancel(self):
        """操作をキャンセル

        待っている応答を切ることはできないので、効くのは次の応答を
        受け取ったところ（応答しない機器では最大約 6 秒後）。
        終わったら result_ready ではなく cancelled が出る。
        """
        self._cancelled = True
    
    def _perform_get(self) -> List[Tuple[str, str, str]]:
        """SNMP GETを実行"""
        host = self.params['host']
        port = self.params.get('port', 161)
        oids = self.params['oids']  # リスト
        version = self.params.get('version', 'v2c')
        
        # 認証情報の準備
        auth_data = self._prepare_auth_data(version)
        
        # SNMP GET実行（OID から ObjectType を組むのも _snmp_get の中）
        errorIndication, errorStatus, errorIndex, varBinds = _snmp_get(
            auth_data, host, port, oids)
        
        if errorIndication:
            raise Exception(f"SNMP Error: {errorIndication}")
        elif errorStatus:
            raise Exception(f"SNMP Error: {errorStatus.prettyPrint()}")
        
        # 結果を整形 [(OID, Type, Value), ...]
        results = []
        for varBind in varBinds:
            oid = varBind[0].prettyPrint()
            value = _value_text(varBind[1])
            value_type = varBind[1].__class__.__name__
            results.append((oid, value_type, value))
        
        return results
    
    def _perform_walk(self) -> List[Tuple[str, str, str]]:
        """SNMP WALKを実行"""
        host = self.params['host']
        port = self.params.get('port', 161)
        oid = self.params['oid']
        version = self.params.get('version', 'v2c')
        
        # 認証情報の準備
        auth_data = self._prepare_auth_data(version)
        
        # SNMP WALK実行。取り消されたときに run() がそこまでの行を
        # 渡せるよう、self._collected へ直接貯める
        results = self._collected
        count = 0
        
        # 途中で抜けても（取り消し・途中のエラー・例外）、WALK が使っている
        # イベントループとソケットをその場で閉じるよう、必ず close() する
        with contextlib.closing(_snmp_walk(auth_data, host, port, oid)) as responses:
            for (errorIndication, errorStatus, errorIndex, varBinds) in responses:
                if self._cancelled:
                    break
            
                if errorIndication or errorStatus:
                    reason = (str(errorIndication) if errorIndication
                              else errorStatus.prettyPrint())
                    if not results:
                        # 1件も取れていない。見せるものが無いのでエラーのまま
                        raise Exception(f"SNMP Error: {reason}")
                    # 取れた分は捨てない。WALK は OID のステップごとに1往復で、
                    # 1リクエストあたり最大6秒（timeout 1 秒 x retries 5）待つ。
                    # ステップ数の多いウォークほど途中で1回落ちる確率が上がる
                    # ので、そこまでの成果を捨てると成果がゼロになりやすい。
                    # 不完全であることは呼び出し側が伝える。
                    self._partial_reason = reason
                    break
            
                for varBind in varBinds:
                    oid_str = varBind[0].prettyPrint()
                    value = _value_text(varBind[1])
                    value_type = varBind[1].__class__.__name__
                    results.append((oid_str, value_type, value))
                    count += 1
            
                # 進捗更新（100件ごと）
                if count % 100 == 0:
                    self.progress_update.emit(f"{count}件取得中...")
        
        return results
    
    def _prepare_auth_data(self, version: str):
        """バージョンに応じた認証データを準備"""
        if version == 'v1':
            community = self.params.get('community', 'public')
            return CommunityData(community, mpModel=0)
        
        elif version == 'v2c':
            community = self.params.get('community', 'public')
            return CommunityData(community, mpModel=1)
        
        elif version == 'v3':
            # SNMPv3パラメータ
            username = self.params.get('username', '')
            auth_protocol = self.params.get('auth_protocol', 'none')
            auth_password = self.params.get('auth_password', '')
            priv_protocol = self.params.get('priv_protocol', 'none')
            priv_password = self.params.get('priv_password', '')

            auth_proto, priv_proto = resolve_v3_protocols(auth_protocol, priv_protocol)
            password_error = v3_password_error(auth_protocol, auth_password,
                                               priv_protocol, priv_password)
            if password_error:
                raise ValueError(password_error)

            # authProtocol / privProtocol は必ず明示的に渡す。pysnmp は
            # authKey だけ渡すと既定で MD5、privKey だけなら既定で DES を選ぶため
            # （7.1.28 でも同じ。pysnmp/hlapi/v3arch/asyncio/auth.py の
            # UsmUserData.__init__、417・429 行目）。
            if auth_protocol == 'none':
                return UsmUserData(username)
            if priv_protocol == 'none':
                return UsmUserData(username, auth_password, authProtocol=auth_proto)
            return UsmUserData(
                username,
                auth_password,
                priv_password,
                authProtocol=auth_proto,
                privProtocol=priv_proto
            )
        
        else:
            raise Exception(f"サポートされていないSNMPバージョン: {version}")


class _TrapBacklog:
    """受信 1 回ぶんの、GUI へ渡したまま届いていない Trap の数え役

    保持件数の上限（パネルの max_traps）が効くのは GUI が一覧へ入れた後だけで、
    受信スレッドから GUI へ渡す Qt のキューには上限が無い。上限が無かった
    ころの実測（pysnmp 7.1.28、127.0.0.1 への UDP）: GUI が止まっている間は
    配送 0 のまま溜まり、1 件あたり約 2.8 KB（普通の Trap）、細工した 60 KB の
    値では 67〜128 KB で、毎秒 5〜7 MB（最大約 50 MB）ずつ増えた。描画ありの
    定常状態でも、毎秒 5,000 件を送ると未処理が約 600〜900 件（最大 1,435 件）
    まで伸びた。捌いた後に OS へ戻ったのは増えた分の約 43〜83%。
    Syslog の max_pending_messages と同じく、上限に達している間に届いた Trap は
    捨てて数える（嵐の始まりの方が残る）。

    SNMPManager が受信の開始ごとに作って受信機へ渡す。受信機は開始のたびに
    作り直す QThread なので、受信機に数えさせて自分へつなぐ形は取らない。
    配送待ちの Trap はこの参照を一緒に運ぶので、止めて開き直したあとに前の回の
    配送が届いても、新しい回の数には混ざらない。take は受信スレッド、delivered
    は GUI スレッドで呼ぶので、数は _lock で守る。
    """

    def __init__(self, limit):
        self.limit = limit
        self._lock = threading.Lock()
        self.pending = 0
        self.dropped = 0          # この回で捨てた件数の合計
        self._reported = 0        # そのうちパネルへ知らせた件数
        # まだ要約していない件数と、その最初・最後に捨てた時刻
        self._burst = 0
        self._burst_first = None
        self._burst_last = None
        # 終了で配送待ちを数え終えたか（discard_pending。GUI スレッドだけが触る）
        self.closed = False

    def take(self):
        """1 件ぶんの枠を取る（受信スレッド）。上限に達していれば数えて False"""
        with self._lock:
            if self.pending < self.limit:
                self.pending += 1
                return True
            now = datetime.now()
            self.dropped += 1
            self._burst += 1
            if self._burst == 1:
                self._burst_first = now
            self._burst_last = now
            started = self._burst == 1
        if started:
            # 受信スレッドの通知コールバックの中で書く。出力先へ書けなくても
            # （容量不足など）例外を pysnmp へ出さない（出すと、その Trap の
            # pysnmp の後始末（送信元の控えの削除）が飛び、あふれ始めのたびに
            # 1 件ずつ残る。実測）
            try:
                print("[SNMPManager] Trap backlog reached its limit (%d); dropping "
                      "newly received traps until the GUI catches up" % self.limit)
            except Exception:
                pass
        return False

    def delivered(self):
        """GUI が 1 件受け取ったので配送待ちを戻す（GUI スレッド）

        Returns:
            (まだ知らせていない捨てた件数, 最後に捨てた時刻,
             捌け切ったときの要約 (件数, 最初, 最後) か None)
        """
        with self._lock:
            if self.pending > 0:
                self.pending -= 1
            new, self._reported = self.dropped - self._reported, self.dropped
            summary = None
            # 要約の区切りは、件数を取り出すのと同じ錠の中で付ける（Syslog・
            # FTP・SFTP と同じ。錠の外だと、その隙に捨てた分が要約から漏れる）
            if self.pending == 0 and self._burst:
                summary = (self._burst, self._burst_first, self._burst_last)
                self._burst = 0
            return new, self._burst_last, summary

    def summarise(self):
        """まだ要約していない分を、捌け切るのを待たずに区切る（GUI スレッド）

        受信を止める・開き直すときに呼ぶ。捌け切るのを待つと、配送待ちが
        配られないまま終わったときに件数がログに残らない（実測: 更新の
        適用は閉じたあと QApplication.quit() で終わり、配送待ち 1000 件が
        配られず、捨てた 500 件の要約が出なかった）。パネルへ知らせる件数
        （_reported）はそのまま配送で知らせる。

        Returns:
            (件数, 最初, 最後) か None
        """
        with self._lock:
            if not self._burst:
                return None
            summary = (self._burst, self._burst_first, self._burst_last)
            self._burst = 0
            return summary

    def discard_pending(self):
        """配送待ちを、配らずに捨てる分として数え終える（終了処理。GUI スレッド）

        以後に届いた配送は一覧へ入れない（SNMPManager._on_trap_queued が
        closed を見る）。捨てた分として数えた Trap を、表示した分にも数えない。
        上限で捨てた件数（dropped と要約）には触らない。

        Returns:
            受信スレッドが GUI へ渡したが、まだ配られていない件数
        """
        with self._lock:
            count, self.pending = self.pending, 0
            self.closed = True
            return count


class SNMPTrapReceiver(QThread):
    """
    SNMP Trap受信スレッド

    pysnmp のエンジン（SnmpEngine + ntfrcv + AsyncioDispatcher）で受信する。
    v3 は scopedPDU が暗号化され得るため、生ソケットで BER デコードする方式では
    中身を取り出せない。USM の復号経路を持つエンジンに載せる必要がある。

    pysnmp 7 は asyncio だけで動く。スレッドとイベントループの持ち方:
    - bind()（呼び出し元スレッド）: 待ち受けソケットを自分で作って同期で
      バインドし、エンジン・認証情報・observer・NotificationReceiver を
      用意する。どれもイベントループを使わないので、呼び出し元スレッドには
      ループを作らない。
    - run()（受信スレッド）: このスレッド専用のループを作ってスレッドの
      ループにし、ディスパッチャとトランスポートを載せてソケットを渡し、
      run_dispatcher() で回す。
    - stop()（どのスレッドからでも）: 回っているループへ
      call_soon_threadsafe で loop.stop を積む。
    - run() の finally: ディスパッチャ・トランスポート・ソケット・ループを閉じる。
    """

    # シグナル定義
    trap_received = pyqtSignal(dict)  # Trap情報（数え役なしで作ったとき）
    # 数え役（_TrapBacklog）つきで作ったときの配送。(数え役, Trap情報)。
    # 数え役を一緒に運ぶので、届いた側はどの回の配送待ちを戻すか取り違えない
    trap_queued = pyqtSignal(object, dict)
    error_occurred = pyqtSignal(str)  # エラーメッセージ
    started = pyqtSignal()  # 開始通知
    stopped = pyqtSignal()  # 停止通知

    def __init__(self, port: int = 162, communities: List[str] = None,
                 v3_users: List[dict] = None, backlog=None):
        """
        Args:
            port: 受信ポート
            communities: 許可する v1/v2c コミュニティ名のリスト
            v3_users: v3 ユーザの定義リスト。各要素は
                {"username", "auth_protocol", "auth_password",
                 "priv_protocol", "priv_password", "engine_ids"}
            backlog: 配送待ちの数え役（_TrapBacklog）。渡すと trap_queued で
                上限つきで渡し、渡さなければ trap_received で全件を渡す
        """
        super().__init__()
        self.port = port
        self._backlog = backlog
        # None（未指定）と []（v1/v2c を受けない）は別物。or で書くと
        # [] が既定値へ落ちるため、Trap のバージョンに v3 を選んで
        # パネルが [] を渡しても public の v1/v2c Trap が通ってしまう。
        self.communities = (['public'] if communities is None
                            else _clean_communities(communities))
        self.v3_users = list(v3_users or [])
        # v3 ユーザ名 → 登録時に求めたセキュリティレベル（1=noAuthNoPriv,
        # 2=authNoPriv, 3=authPriv）。pysnmp は Trap 受信側（非 authoritative）で
        # 最低レベルの検査をしないため、こちらで見る
        self._min_level = {}
        self._warned_weak = set()
        self._running = False
        self._engine = None
        self._transport = None
        # bind() で作った待ち受けソケット。run() が asyncio へ渡す
        self._socket = None
        # run() が作ったイベントループ。閉じたあとも参照は残す（次の run() で
        # 置き換わる）
        self._loop = None
        # _dispatching と停止要求は別スレッドから触るのでロックで守る。
        # _dispatching は run_dispatcher() に入る直前から抜けるまで True で、
        # その間だけ stop() はループへ停止を積む。それより前に来た停止要求は
        # _stop_requested に残り、run() がループを回し始める前に見る。
        # （pysnmp 5.1.0 ではジョブ ID で止めていて、jobStarted より先に
        # jobFinished を呼ぶと runDispatcher() が永久に戻らなくなった。）
        self._state_lock = threading.Lock()
        self._dispatching = False
        self._stop_requested = False
        # observer で拾った直近のセキュリティ情報（表示用の参考値）
        self._last_security = {}

    def bind(self) -> bool:
        """待ち受けを用意する。

        呼び出し元スレッドで実行し、失敗を戻り値で返す。スレッドの中で
        バインドすると成否を呼び出し側へ返せず、ポートが使用中でも UI は
        「受信中」の表示のまま何も待ち受けない状態になる。

        pysnmp 7 のトランスポートは、バインドを asyncio の中で非同期に行う
        （open_server_mode は create_datagram_endpoint を ensure_future に
        積むだけ。7.1.28 の pysnmp/carrier/asyncio/dgram/base.py 160〜183 行目）
        ので、そのままではここで成否を返せない。ソケットは自分で作って同期で
        バインドし、run() で asyncio へ渡す。

        ここで作るエンジン・認証情報・observer・NotificationReceiver は
        イベントループを使わない（7.1.28 の SnmpEngine.__init__・
        config.add_v1_system / add_v3_user・ntfrcv に asyncio の呼び出しは
        無い。ループを使うのはディスパッチャとトランスポートを作るときで、
        それは run() が受信スレッドで行う）。呼び出し元スレッドには
        ループを作らない。
        """
        if not _PYSNMP_AVAILABLE:
            self.error_occurred.emit("SNMPライブラリ(pysnmp)を利用できません")
            return False

        # try の中で落とすと「ポート N で待ち受けできません」に化けて、
        # ポート競合を探しに行かせてしまう。原因が分かる形で先に止める。
        for user in self.v3_users:
            password_error = v3_password_error(
                user.get("auth_protocol", "none"), user.get("auth_password", ""),
                user.get("priv_protocol", "none"), user.get("priv_password", ""))
            if password_error:
                self.error_occurred.emit(f"v3 ユーザの設定に問題があります: {password_error}")
                return False

        # 前に bind() したまま start() していない待ち受けがあれば閉じてから作る。
        # 参照を上書きすると、そのソケットが GC されるまでポートを掴んだまま残る。
        # 受信スレッドの中（再 start 時の run()）では前回の finally で閉じ済み。
        if not self.isRunning():
            self._close_engine()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket = sock
            # Windows の REUSEADDR 系は待ち受け中のポートへの二重バインドを
            # 許すので、他プロセスの待ち受けポートを奪わないよう排他バインドに
            # する（5.1.0 は pysnmp が自前のソケットへ REUSEADDR 系を無条件に
            # 立てていたので、それを戻す意味もあった）。
            set_exclusive_bind(sock)
            sock.bind(('0.0.0.0', self.port))
            sock.setblocking(False)

            self._engine = SnmpEngine()
            self._register_credentials()
            self._register_observer()
            ntfrcv.NotificationReceiver(self._engine, self._on_notification)
            return True
        except Exception as e:
            self._close_engine()
            self.error_occurred.emit(f"ポート {self.port} で待ち受けできません: {e}")
            return False

    def _register_credentials(self):
        """許可するコミュニティと v3 ユーザをエンジンへ登録する"""
        # v1/v2c: コミュニティごとに securityName を分けて登録する。
        # 未登録のコミュニティは pysnmp 側で弾かれる（大文字小文字は区別される）。
        for index, community in enumerate(self.communities):
            config.add_v1_system(self._engine, f"netbelt-v2c-{index}", community)
        # v3: USM ユーザ行のキーは (securityEngineId, securityName) の組なので、
        # engineID ごとに登録が要る。Trap では送信側の機器が authoritative engine
        # になるため、受信側は送信元の engineID を事前に知っている必要がある。
        # securityEngineId を省略した登録や「五つのゼロ」のワイルドカードでは
        # 受信できないことを実測で確認済み。
        from pysnmp.proto.rfc1902 import OctetString

        for user in self.v3_users:
            username = user.get("username", "").strip()
            if not username:
                continue

            auth_proto, priv_proto = resolve_v3_protocols(
                user.get("auth_protocol", "none"), user.get("priv_protocol", "none"))
            auth_key = user.get("auth_password") or None
            priv_key = user.get("priv_password") or None
            # このユーザに求めるレベル。これより弱い通知は _on_notification で捨てる
            if user.get("priv_protocol", "none") != "none":
                self._min_level[username] = 3
            elif user.get("auth_protocol", "none") != "none":
                self._min_level[username] = 2
            else:
                self._min_level[username] = 1

            # securityEngineId 無しでも1回登録する（送信用・将来の INFORM 用）。
            # 公式サンプル multiple-usm-users.py と同じ構成。
            security_engine_ids = [None]
            for engine_id in user.get("engine_ids", []):
                # 設定が壊れていて文字列以外が来ても、受信そのものを
                # 止めない（その要素だけ捨てる）
                if not isinstance(engine_id, str) or not engine_id.strip():
                    continue
                security_engine_ids.append(
                    OctetString(hexValue=engine_id.strip()))

            for security_engine_id in security_engine_ids:
                config.add_v3_user(
                    self._engine, username,
                    auth_proto, auth_key,
                    priv_proto, priv_key,
                    securityEngineId=security_engine_id)

    def _register_observer(self):
        """受信メッセージのセキュリティ情報を拾う

        ntfrcv のコールバックには securityName / securityLevel が渡らない
        ため、observer で別途拾う。pysnmp は observer を processPdu の
        直前に発火して直後に消すので、同一スレッド・同一コールスタックの
        あいだだけ有効な値になる（Trap が近接しても取り違えない）。
        7.1.28 でも同じ（pysnmp/proto/rfc3412.py の receive_message が
        'rfc3412.receiveMessage:request' で store_execution_context し
        （536 行目）、process_pdu のあとで clear_execution_context する
        （575 行目））。

        observer は pysnmp のディスパッチ経路の中で呼ばれ、pysnmp 5.1.0 では
        ここで例外が出ると runDispatcher() を抜けて受信が完全に止まった。
        旧実装は受信ループの中で握っていたので1パケットで止まることは
        無かった。_on_notification と同じ扱いに揃える。7.1.28 では受信の
        処理は loop.call_soon のコールバックとして走る
        （pysnmp/carrier/asyncio/dgram/base.py 108 行目）ので、例外は
        asyncio の例外ハンドラに記録されるだけで受信は続くが、前の Trap の
        値を残さないため、ここで握る扱いは変えない。
        """
        def _observe(snmp_engine, execpoint, variables, cb_ctx):
            # 先に空にする。失敗したときに前の Trap の値が残ると、
            # 別の Trap のセキュリティ情報を今の Trap として表示してしまう。
            self._last_security = {}
            try:
                self._capture_security(variables)
            except Exception as e:
                print(f"[SNMPTrapReceiver] セキュリティ情報を拾えません: {e}")

        self._engine.observer.register_observer(
            _observe, 'rfc3412.receiveMessage:request')

    def _capture_security(self, variables):
        """observer から渡された変数を _last_security へ写す"""
        self._last_security = {
            'security_name': str(variables.get('securityName', '')),
            'security_level': str(variables.get('securityLevel', '')),
            'security_model': str(variables.get('securityModel', '')),
        }
        # 送信元のアドレスとポート。旧実装は recvfrom の addr をそのまま
        # 使っていたので、エンジン化で意味が変わらないようここで拾う。
        address = variables.get('transportAddress')
        self._last_security['source_ip'] = str(address[0]) if address else ''
        self._last_security['source_port'] = int(address[1]) if address else 0

    def _on_notification(self, snmp_engine, state_reference,
                         context_engine_id, context_name, var_binds, cb_ctx):
        """ntfrcv から呼ばれる通知コールバック

        pysnmp 5.1.0 の ntfrcv はコールバックのアリティを例外ベースで判定し、
        TypeError が出ると「引数の数が違う」とみなして呼び直した。本体で
        TypeError を漏らすと同じ通知が二度処理されるため、ここで握りつぶす。
        7.1.28 の ntfrcv は 6 引数で 1 回呼ぶだけで呼び直さない
        （pysnmp/entity/rfc3413/ntfrcv.py の process_pdu、139 行目）が、
        例外をディスパッチ経路へ漏らさない扱いとして残す。

        GUI へは _emit_trap で渡す。SNMPManager が作った受信機では、配送待ちが
        上限（max(1000, max_traps)）に達している間に届いた Trap を捨てて数える
        （理由と上限が無かったころの実測は _TrapBacklog）。
        制限: 数えられるのは自分の配送待ちで捨てた分だけ。それより手前の
        OS の受信バッファで落ちた分は見えない（実測: 毎秒 2,000 件前後から
        落ち始め、毎秒 5,000 件を 10 秒送ると 49,998 件中 23,781 件しか
        届かなかった）。
        """
        try:
            if self._is_weaker_than_registered():
                return
            self._emit_trap(self._build_trap_data(var_binds))
        except TypeError as e:
            print(f"[SNMPTrapReceiver] 通知処理エラー: {e}")
        except Exception as e:
            print(f"[SNMPTrapReceiver] 通知処理エラー: {e}")

    def _emit_trap(self, trap_data):
        """GUI へ 1 件渡す。数え役があれば、配送待ちが上限の間は捨てて数える"""
        backlog = self._backlog
        if backlog is None:
            self.trap_received.emit(trap_data)
        elif backlog.take():
            self.trap_queued.emit(backlog, trap_data)

    def _is_weaker_than_registered(self) -> bool:
        """v3 の通知が、登録時に求めたレベルより弱ければ True。

        pysnmp の USM は、受信側が authoritative でない Trap では最低
        securityLevel の検査を行わない。authPriv で登録したユーザ名に対して
        鍵を付けない noAuthNoPriv の通知を送ると、そのまま届く（実測）。
        ユーザ名と engineID は秘密ではないので、鍵を知らない送信者が偽の
        Trap を一覧へ記録させられる。登録レベル未満は捨てる。
        """
        sec = self._last_security
        if str(sec.get('security_model', '')) != '3':
            return False
        name = sec.get('security_name', '')
        required = self._min_level.get(name)
        if required is None:
            return False
        try:
            level = int(sec.get('security_level') or 0)
        except ValueError:
            level = 0
        if level >= required:
            return False
        # 偽の通知で画面が埋まらないよう、ユーザ名ごとに一度だけ知らせる
        if name not in self._warned_weak:
            self._warned_weak.add(name)
            self.error_occurred.emit(
                "v3 ユーザ %s に、登録より弱いセキュリティレベル（%d < %d）の"
                "通知が届いたため捨てました（送信元 %s）"
                % (name, level, required, sec.get('source_ip', '')))
        return True

    def _build_trap_data(self, var_binds) -> dict:
        """
        受信した VarBinds を trap_data の形へ整える

        Args:
            var_binds: (ObjectName, ObjectSyntax) のシーケンス

        Returns:
            _emit_trap で GUI へ渡す dict（trap_received か trap_queued で送る）
        """
        security = dict(self._last_security)
        trap_data = {
            'source_ip': security.pop('source_ip', ''),
            # 待ち受けポートではなく送信元のポート（旧実装と同じ意味）
            'source_port': security.pop('source_port', 0),
            'timestamp': None,
            'trap_oid': None,
            'varbinds': [],
            'received_at': datetime.now().isoformat(),
        }
        # security_name / security_level / security_model を足す
        trap_data.update(security)

        for oid, val in var_binds:
            oid_str = oid.prettyPrint()
            value_str = val.prettyPrint()
            value_type = val.__class__.__name__

            # 合成されたコミュニティは落とす（送信元アドレスを合成する
            # snmpTrapAddress は障害解析に要るので残す。プロキシ経由だと
            # 送信元 IP と agent-addr は別物になる）
            if oid_str == SNMP_TRAP_COMMUNITY_OID:
                continue

            if oid_str == '1.3.6.1.2.1.1.3.0':      # sysUpTime
                trap_data['timestamp'] = value_str
            elif oid_str == '1.3.6.1.6.3.1.1.4.1.0':  # snmpTrapOID
                trap_data['trap_oid'] = value_str

            trap_data['varbinds'].append({
                'oid': oid_str,
                'value': value_str,
                'type': value_type,
            })

        return trap_data

    def run(self):
        """Trap受信スレッドのメイン処理"""
        loop = None
        try:
            print(f"[SNMPTrapReceiver] 開始: ポート{self.port}")
            if self._engine is None and not self.bind():
                return

            # コミュニティ文字列と v3 のパスワードは実質的な認証情報。
            # 凍結ビルドでは stdout がログへ恒久保存されるため値は出さない。
            print(f"[SNMPTrapReceiver] 許可コミュニティ: {len(self.communities)}件")
            print(f"[SNMPTrapReceiver] v3ユーザ: {len(self.v3_users)}件")
            print(f"[SNMPTrapReceiver] Trap受信待機中...")

            # このスレッド専用のループ。スレッドのループにもするのは、pysnmp が
            # 中で asyncio.get_event_loop() を呼んだときにこのループを返すため。
            # ディスパッチャとトランスポートにはループを明示して渡す
            # （渡さないと作った時点で asyncio.get_event_loop() を呼ぶ。7.1.28 の
            # pysnmp/carrier/asyncio/dispatch.py 65 行目・dgram/base.py 100 行目）。
            loop = _new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            dispatcher = AsyncioDispatcher(loop=loop)
            self._engine.register_transport_dispatcher(dispatcher)
            transport = udp.UdpAsyncioTransport(loop=loop)
            self._transport = transport
            config.add_transport(self._engine, udp.DOMAIN_NAME, transport)

            self._running = True
            self.started.emit()

            with self._state_lock:
                stop_before_start = self._stop_requested
            if not stop_before_start:
                # bind() で用意したソケットを asyncio へ渡す。open_server_mode
                # (sock=...) も中で同じ create_datagram_endpoint を呼ぶが、
                # ensure_future に積むだけで結果を見ないため（7.1.28 の
                # pysnmp/carrier/asyncio/dgram/base.py 172・178 行目）、失敗しても
                # 黙って何も受け取らない受信機になる。ここで完了まで待って失敗を拾う
                loop.run_until_complete(loop.create_datagram_endpoint(
                    lambda: transport, sock=self._socket))
                with self._state_lock:
                    # ここより前に来た停止要求は、まだループへ積めていない
                    stop_before_start = self._stop_requested
                    self._dispatching = not stop_before_start
            if not stop_before_start:
                dispatcher.run_dispatcher()

            # 出力先へ書けなくても（容量不足など）例外を出さない。出すと下の
            # except の行も書けず、例外が run() の外（excepthook）へ出る。
            # main.py の excepthook はこのスレッドのままダイアログを開くので、
            # stop_trap_receiver の wait が時間切れになり、停止が終わらない
            try:
                print(f"[SNMPTrapReceiver] 正常終了")
            except Exception:
                pass

        except Exception as e:
            print(f"[SNMPTrapReceiver] エラー: {str(e)}")
            import traceback
            traceback.print_exc()
            self.error_occurred.emit(f"Trap受信エラー: {str(e)}")

        finally:
            with self._state_lock:
                self._dispatching = False
            self._running = False
            self._close_engine()
            if loop is not None:
                asyncio.set_event_loop(None)
            # 同じ受信機をもう一度 start() したとき、前回の停止要求が
            # 残っていると、ループを回す前に即終了する。
            with self._state_lock:
                self._stop_requested = False
            # 例外で抜けたときも必ず知らせる。ここを成功経路だけに
            # 置くと、受信が死んでも画面は「受信中」のまま残る。
            self.stopped.emit()

    def _close_engine(self):
        """エンジン・トランスポート・待ち受けソケット・イベントループを片付ける

        bind() の失敗時、run() の終わり、bind() だけして start() しなかった
        受信機の stop() から呼ぶ。何度呼んでもよい。回っているループには
        触らない（回っている間は run() が持ち主）。
        """
        engine, self._engine = self._engine, None
        sock, self._socket = self._socket, None
        self._transport = None
        if engine is not None:
            # トランスポートを閉じ（ソケットの close はループを回したときに
            # 実行される）、ディスパッチャのタイマを取り消す
            _close_dispatcher(engine)
        loop = self._loop
        if loop is not None and not loop.is_closed() and not loop.is_running():
            _close_event_loop(loop)
        if sock is not None:
            # asyncio へ渡していなければ、ここで閉じるまで残る
            try:
                sock.close()
            except OSError:
                pass

    def stop(self):
        """Trap受信を停止

        回っている受信ループへ call_soon_threadsafe で loop.stop を積む。
        ループは待っている select() からすぐ起きて run_dispatcher() が戻る
        （7.1.28 の run_dispatcher は loop.run_forever() を呼ぶだけ。
        pysnmp/carrier/asyncio/dispatch.py 77〜84 行目）。

        スレッドがループを回し始める前（run() の準備中）に呼ばれた場合は、
        停止要求を立てるだけにして run() に終わらせてもらう。準備中の
        run_until_complete へ loop.stop が届くと、準備の途中で止まって
        しまう。bind() だけして start() していない受信機なら、ここで
        待ち受けソケットとエンジンを閉じる。
        """
        # 出力先へ書けなくても（容量不足など）例外を出さない（出すと停止を
        # 求める前に抜け、受信スレッドが動き続ける）
        try:
            print(f"[SNMPTrapReceiver] 停止要求")
        except Exception:
            pass
        self._running = False

        with self._state_lock:
            already_requested = self._stop_requested
            self._stop_requested = True
            if self._dispatching:
                # loop.stop を積むのは 1 度だけ。2 つ目が、ループが止まってから
                # run() の finally に入るまでの間に積まれると、後始末
                # （_close_event_loop の run_until_complete）がそれで途中で
                # 止まり、残りの片付けが飛ぶ（実測）。run() は停止要求が
                # 無いときだけ _dispatching を立てるので、立っている間の
                # 最初の stop() だけが積む
                if not already_requested:
                    try:
                        self._loop.call_soon_threadsafe(self._loop.stop)
                    except RuntimeError:
                        pass    # 閉じたループ。run() は既に終わりかけている
                return
            unstarted = not self.isRunning()

        if unstarted:
            self._close_engine()


class SNMPManager(QObject):
    """SNMPマネージャー"""
    
    # シグナル定義
    operation_started = pyqtSignal(str)  # 操作名
    operation_completed = pyqtSignal(bool, object)  # (success, result)
    progress_update = pyqtSignal(str)  # ステータスメッセージ
    # WALK が途中で途切れたときの理由。結果は operation_completed で
    # 普通に届くので、不完全であることだけをこちらで伝える
    operation_partial = pyqtSignal(str)
    # 利用者が止めた GET/WALK の、そこまでに取れた行。この操作では
    # operation_completed は出ない
    operation_cancelled = pyqtSignal(object)
    error_occurred = pyqtSignal(str)  # エラーメッセージ
    trap_received = pyqtSignal(dict)  # Trap受信
    # 配送待ちの上限で捨てた Trap。(新しく捨てた件数, 上限, 最後に捨てた時刻)。
    # 今の回（最後に開始した受信）のぶんだけ出す
    trap_dropped = pyqtSignal(int, int, object)
    trap_receiver_started = pyqtSignal()  # Trap受信開始
    trap_receiver_stopped = pyqtSignal()  # Trap受信停止
    
    # GUI へ渡したまま届いていない Trap の上限（_TrapBacklog）。パネルの
    # max_traps の方が大きければそちらに合わせる。小さくすると、max_traps を
    # 大きくしている利用者に新しく取りこぼしが出る
    DEFAULT_MAX_PENDING_TRAPS = 1000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.trap_receiver = None
        # 停止しきらなかったスレッドを保持する。参照を落とすと実行中の QThread が
        # 破棄されてプロセスごと落ちるため、終わるまで手放さない。
        self._retired_receivers = []
        # 今の回の数え役。止めても次の開始までは残す（止めたあとに届く配送の
        # 取りこぼしもパネルへ知らせる）
        self._trap_backlog = None
        # 前の回の数え役のうち、開き直した時点で配送待ちが残っていたもの。
        # 終了で捨てる件数に入れる（discard_undelivered_traps）
        self._earlier_backlogs = []
    
    def snmp_get(self, host: str, oids: List[str], **kwargs):
        """
        SNMP GET操作を実行
        
        Args:
            host: ホストアドレス
            oids: OIDのリスト
            **kwargs: その他のパラメータ (port, version, community, など)
        """
        # isRunning() で判定してはいけない。result_ready は run() の中から
        # queued で emit されるので、スレッドが終わってから結果がメイン
        # スレッドへ届くまでの間は isRunning() == False かつ結果は未配送。
        # そこで次の要求を受け付けると、あとから届いた前の結果が新しい要求の
        # ものとして扱われる（呼び出し側はホストを取り違えて記録する）。
        # 参照を手放すのは finished を受けたときなので、未配送の結果がある間は
        # 必ず None ではない。
        if self.worker is not None:
            self.error_occurred.emit("既に操作が実行中です")
            return False

        params = {
            'host': host,
            'oids': oids,
            **kwargs
        }
        
        self.worker = SNMPWorker('get', params)
        self.worker.result_ready.connect(self._on_operation_completed)
        self.worker.finished.connect(self._on_worker_finished)
        # 中継は必ずシグナル同士でつなぐ（.emit を渡さない）。
        # self.<シグナル> は参照のたびに作られるその場限りの
        # pyqtBoundSignal で、その .emit を渡すと PyQt は受け手が
        # この QObject だと認識できない。別スレッドから積まれた呼び出しが
        # キューに残ったままこのオブジェクトが解放されると、次に誰かが
        # processEvents() した時点で解放済みの C++ を叩いて落ちる（実測）。
        self.worker.progress_update.connect(self.progress_update)
        self.worker.partial_result.connect(self.operation_partial)
        self.worker.cancelled.connect(self.operation_cancelled)
        self.worker.start()

        self.operation_started.emit(f"SNMP GET: {host}")
        return True   # 受理した（実行中で断った場合は False）
    
    def snmp_walk(self, host: str, oid: str, **kwargs):
        """
        SNMP WALK操作を実行
        
        Args:
            host: ホストアドレス
            oid: 開始OID
            **kwargs: その他のパラメータ (port, version, community, など)
        """
        # 未配送の結果がある間は受け付けない（理由は snmp_get と同じ）
        if self.worker is not None:
            self.error_occurred.emit("既に操作が実行中です")
            return False

        params = {
            'host': host,
            'oid': oid,
            **kwargs
        }
        
        self.worker = SNMPWorker('walk', params)
        self.worker.result_ready.connect(self._on_operation_completed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.progress_update.connect(self.progress_update)
        self.worker.partial_result.connect(self.operation_partial)
        self.worker.cancelled.connect(self.operation_cancelled)
        self.worker.start()

        self.operation_started.emit(f"SNMP WALK: {host} - {oid}")
        return True   # 受理した（実行中で断った場合は False）

    def request_cancel(self) -> bool:
        """実行中の GET/WALK に取り消しを頼む（待たずに戻る）

        画面の停止操作から呼ぶ。cancel_operation() は終了処理用で、
        スレッドの終了を最大 5 秒待つので GUI が固まる。
        取り消しは次の応答を受けたところで効き（応答しない機器では最大
        約 6 秒）、そこまでに取れた行が operation_cancelled で届く。
        それまでは self.worker を持ったままなので、新しい要求は実行中と
        同じく断られる。

        Returns:
            頼めたら True。何も走っていない、または結果が既に配送待ちなら False
        """
        worker = self.worker
        if worker is None or not worker.isRunning():
            return False
        worker.cancel()
        return True

    def cancel_operation(self):
        """現在の操作をキャンセル

        制限: 5 秒で待つのをやめ、終わらなかったことは警告を出すだけで
        呼び出し側へは返さない。応答しない機器への GET/WALK は既定で
        約 6 秒かかるので、この待ちは実際に超える。超えてもスレッドは
        self.worker が参照を持ったまま残り、終了処理を続けても実測では
        異常終了しない（終了コード 0）。
        """
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            # タイムアウト無しで待つと、WALK 中はキャンセルフラグを見るまでの間
            # GUI が固まる。待てなければ参照を保持したまま戻る。
            if not self.worker.wait(5000):
                print("[SNMPManager] 警告: 操作が5秒以内に終了しませんでした")
    
    def _on_operation_completed(self, success: bool, result):
        """操作完了時の処理"""
        self.operation_completed.emit(success, result)
        # ここで self.worker = None にしてはいけない。result_ready は run() の
        # 中から emit されるため、この時点でスレッドはまだ実行中であり、
        # 最後の参照を落とすと QThread が実行中に破棄されて異常終了する。
        # 参照の解放は finished シグナル（run() が返った後に出る）で行う。

    def _on_worker_finished(self):
        """ワーカースレッドが実際に終了してから参照を解放する。"""
        if self.sender() is self.worker:
            self.worker = None
    
    def is_busy(self) -> bool:
        """操作が実行中かどうか"""
        return self.worker is not None and self.worker.isRunning()
    
    def fix_firewall(self, port: int = 162):
        """手動: Windows FW 受信許可を追加（管理者昇格/UAC）

        起動時には触らない（TFTP/FTP と同じ 3CDaemon 方式）。Windows の
        初回プロンプトを拒否したなどで Trap が届かない環境の復旧用で、
        押したときだけ昇格する。
        """
        try:
            from .firewall import (combine_results, ensure_inbound_allow,
                                   ensure_self_program_allow)
            ok, msg = ensure_inbound_allow("SNMP Trap", "UDP", port)
            print(f"[SNMP] ファイアウォール: {msg}")
            ok2, msg2 = ensure_self_program_allow()
            print(f"[SNMP] ファイアウォール(自exe): {msg2}")
            # 失敗した操作の理由を返す。最初の msg を決め打ちで返すと、
            # 自exe の許可だけ失敗したとき成功の文言が出る
            return combine_results([(ok, msg), (ok2, msg2)])
        except Exception as e:
            print(f"[SNMP] ファイアウォール設定エラー: {e}")
            return False, str(e)

    def start_trap_receiver(self, port: int = 162, communities: List[str] = None,
                            v3_users: List[dict] = None, keep_traps: int = 0):
        """
        SNMP Trap受信を開始
        
        Args:
            port: 受信ポート (デフォルト: 162)
            communities: 許可するコミュニティ名のリスト
            v3_users: v3 ユーザの定義リスト（SNMPTrapReceiver の docstring 参照）
            keep_traps: パネルが保持する Trap の件数（max_traps）。配送待ちの
                上限は max(DEFAULT_MAX_PENDING_TRAPS, keep_traps)
        """
        if self.trap_receiver and self.trap_receiver.isRunning():
            self.error_occurred.emit("既にTrap受信が実行中です")
            return False
        # 前の回を置き換える前に書く（停止を通らずに受信スレッドが終わった回）
        self._summarise_trap_backlog()
        
        # ファイアウォールは自動設定しない（3CDaemon 方式）。管理者昇格(UAC)を避けるため、
        # 受信許可は Windows 標準の初回プロンプト／既存の許可ルールに委ねる。
        # 自動で足すと、ポートを変えて使うたびポート名入りのルールが恒久登録され、
        # 停止しても消えずに残骸が増える。通らない環境は fix_firewall() で直す。
        print("[SNMP] ファイアウォール: 自動設定なし（Windowsの許可に委ねます）")
        backlog = _TrapBacklog(max(self.DEFAULT_MAX_PENDING_TRAPS, keep_traps or 0))
        self.trap_receiver = SNMPTrapReceiver(port, communities, v3_users,
                                              backlog=backlog)
        # 中継は必ずシグナル同士でつなぐ（.emit を渡さない）。
        # self.<シグナル> は参照のたびに作られるその場限りの
        # pyqtBoundSignal で、その .emit を渡すと PyQt は受け手が
        # この QObject だと認識できない。別スレッドから積まれた呼び出しが
        # キューに残ったままこのオブジェクトが解放されると、次に誰かが
        # processEvents() した時点で解放済みの C++ を叩いて落ちる（実測）。
        # Trap は配送待ちを戻してから流すので、自分のメソッドで受ける
        # （QObject のメソッドなら、この QObject が消えたときに接続ごと外れる）。
        self.trap_receiver.trap_queued.connect(self._on_trap_queued)
        self.trap_receiver.error_occurred.connect(self.error_occurred)
        self.trap_receiver.started.connect(self.trap_receiver_started)
        self.trap_receiver.stopped.connect(self.trap_receiver_stopped)
        # スレッドを起こす前にバインドし、失敗ならここで打ち切る
        if not self.trap_receiver.bind():
            self.trap_receiver = None
            return False
        self._earlier_backlogs = [
            old for old in self._earlier_backlogs + [self._trap_backlog]
            if old is not None and old.pending]
        self._trap_backlog = backlog
        self.trap_receiver.start()
        
        self.operation_started.emit(f"SNMP Trap受信開始: ポート{port}")
        return True
    
    def _on_trap_queued(self, backlog, trap_data):
        """受信機からの Trap を流す（GUI スレッドで動く）。届いたぶん配送待ちを戻す"""
        if backlog.closed:
            return   # 終了で捨てた分として数え終えた（discard_undelivered_traps）
        new, last, summary = backlog.delivered()
        self.trap_received.emit(trap_data)
        if summary:
            self._log_dropped_traps("Trap backlog drained", summary,
                                    backlog.limit)
        # 前の回のぶんは、開始で 0 に戻したパネルの表示へ足さない（ログには残る）
        if new and backlog is self._trap_backlog:
            self.trap_dropped.emit(new, backlog.limit, last)

    @staticmethod
    def _log_dropped_traps(head, summary, limit):
        """捨てた件数の要約を 1 行書く

        README は「Trap backlog」を含む行を見るよう案内している。
        出力先へ書けなくても（容量不足など）例外は出さない。呼び出し元は
        この後でパネルへ件数を知らせる・受信の停止を終える（_on_trap_queued・
        stop_trap_receiver）ので、書けないのはこの行だけにする。
        """
        count, first, last = summary
        try:
            print("[SNMPManager] %s: dropped %d trap(s) between %s and %s "
                  "(limit %d)" % (head, count, first.strftime("%H:%M:%S"),
                                  last.strftime("%H:%M:%S"), limit))
        except Exception:
            pass

    def _summarise_trap_backlog(self):
        """今の回の、まだ要約していない取りこぼしをすぐログへ書く

        停止と開始（受信スレッドが自分で終わった後の開き直し）で呼ぶ。
        受信スレッドが終わっていれば、この回でこれ以上は捨てない。
        配送待ちはこのあとも届き、パネルの件数はそこで足す
        （_TrapBacklog.summarise）。

        制限: 受信スレッドが 5 秒で止まらずに残った場合（stop_trap_receiver の
        警告）、その後に捨てた分は新しいあふれとして数え、捌け切ったときか
        次の開始で書く。それより先にアプリが終わると、その分は残らない。
        """
        backlog = self._trap_backlog
        summary = backlog.summarise() if backlog is not None else None
        if summary:
            self._log_dropped_traps(
                "Reception stopped before the Trap backlog drained", summary,
                backlog.limit)

    def discard_undelivered_traps(self):
        """終了で配られずに消える Trap の件数を、配送を待たずにログへ書く

        MainWindow.closeEvent で受信を止めた後、記録の書き切りより前に呼ぶ
        （書き切りは配送待ちをその場で配る）。受信スレッドが GUI へ渡したが
        まだ配られていない Trap は、更新の適用（閉じたあと QApplication.quit()）
        では配られずに消え、×で閉じたときは閉じた窓の一覧へ入るだけで、
        どちらも誰にも見えない（実測: 配送待ち 1000 件で、更新の適用は 1 件も
        配らずに終わり、×は閉じた後に 1000 件を一覧へ入れてから終わった。
        配っている途中に更新を適用すると、quit() の後も残りを配った。端末の
        記録中は、どちらの終わり方でも書き切りの中で一覧へ全部配った）。
        件数だけを書いて、以後に届いた配送は一覧へ入れない（同じ Trap を
        表示と破棄の両方に数えない）。

        上限で捨てた件数（「Trap backlog」の行）とは別の行にする。0 件のときは
        書かない（ほとんどの終了は配送待ちが無いので、ログを今までと変えない。
        上限の行も捨てたときだけ書く）。

        制限: 受信スレッドが 5 秒で止まらずに残った場合（stop_trap_receiver の
        警告）、ここより後に受け取った分は表示も数えもしない。
        """
        backlogs = self._earlier_backlogs + [self._trap_backlog]
        count = sum(b.discard_pending() for b in backlogs if b is not None)
        if count:
            print("[SNMPManager] Discarded %d undelivered trap(s) at exit "
                  "(not shown in the list)" % count)

    def stop_trap_receiver(self):
        """SNMP Trap受信を停止

        isRunning() で分岐しないのは、スレッドが既に自分で終わっている場合にも
        参照を片付ける必要があるため（放置すると次回の起動判定が狂う）。
        """
        receiver = self.trap_receiver
        if receiver is None:
            return

        receiver.stop()
        finished = receiver.wait(5000)  # 停止要求は受信ループをすぐ起こす
        if not finished:
            # terminate() は任意の位置でスレッドを殺すためソケットや内部状態が
            # 壊れる。ここでは強制終了せず、参照を保持して破棄だけ防ぐ
            # （実行中の QThread を破棄するとプロセスごと落ちる）。
            # 出力先へ書けなくても（容量不足など）例外を出さない（出すと
            # 参照の保持・取りこぼしの要約・停止の知らせが飛ぶ）
            try:
                print("[SNMPManager] 警告: Trap受信スレッドが5秒以内に終了しませんでした。"
                      "強制終了はせず、終了するまで参照を保持します")
            except Exception:
                pass
            self._retire(receiver)

        # 配送待ちが捌け切るのを待たずに書く。アプリの終了で配られずに
        # 終わっても、捨てた件数はログに残る
        self._summarise_trap_backlog()
        self.trap_receiver = None
        self.operation_started.emit("SNMP Trap受信停止")

    def _retire(self, receiver):
        """止まりきらなかった受信スレッドを、終わるまで手放さずに持つ

        実行中の QThread を破棄するとプロセスごと落ちるため参照を残すが、
        刈らないと単調に増え続け、終了時に実行中のスレッドを抱えたまま
        プロセスが終わる。終わったら自分で外れるようにしておく。
        """
        self._retired_receivers.append(receiver)
        receiver.finished.connect(lambda: self._forget(receiver))
        # wait() が諦めた直後、connect を張る前に終わっていると
        # finished を取り逃す。ここで見ておけばどちらの順でも外れる。
        if receiver.isFinished():
            self._forget(receiver)

    def _forget(self, receiver):
        """終わった受信スレッドを保持リストから外す"""
        if receiver in self._retired_receivers:
            self._retired_receivers.remove(receiver)
    
    def is_trap_receiver_running(self) -> bool:
        """Trap受信が実行中かどうか"""
        return self.trap_receiver is not None and self.trap_receiver.isRunning()
