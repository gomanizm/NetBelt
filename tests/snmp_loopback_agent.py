"""SNMP の実通信テストで使う、127.0.0.1 だけで動く応答側と Trap 送信側。

pyasn1 の脆弱性（GHSA-8ppf-4f7h-5ppj / CVE-2026-59885）対応で pysnmp を
5.1.0 から 7.1.28 へ移した（最初は 7.1.30 で進めたが、7.1.30 の AES は
cryptography 47 以上にしか無いモジュールを読み、据え置いた cryptography
46.0.3 では AES が黙って無効になったため 7.1.28 にした）。7 系は asyncore が
無く asyncio だけになり、GET/WALK/Trap の呼び出し方がまるごと変わった。
それまでの GET/WALK のテストは nextCmd / getCmd を差し替える mock が中心で、
実際の BER の符号化・復号も、USM の鍵の導出と暗号化も一度も通って
いなかった。

ここには NetBelt のコードを一切使わない、pysnmp 7.1.28 だけで組んだ
相手役を置く。テストは NetBelt の公開の形（SNMPManager / SNMPWorker /
SNMPTrapReceiver）をこの相手役に向けて動かす。

- LoopbackAgent: コマンドレスポンダ（GET / GETNEXT / GETBULK）。専用の
  スレッドと専用のイベントループで 127.0.0.1 の空きポート（0 で bind）に
  立てる。値は MIB モジュールではなく、自前の MIB 計装（dict）から返す。
  VACM は通さない（計装が acFun を呼ばない）ので、コミュニティと v3 の
  ユーザを登録するだけで全部読める。受信を「保留」「破棄」して、途中で
  応答が止まる機器や、応答を待っている間の取り消しを作れる。
  受けた v3 要求の securityLevel を記録する（security_levels）。VACM を
  通さなくても、応答側の USM が、登録と違う securityLevel の要求を
  unsupportedSecurityLevel で断る（7.1.28 の pysnmp/proto/secmod/rfc3414/
  service.py 1202〜1240 行目。authoritative の側の検査）。authPriv で登録した
  ユーザへ authNoPriv で問い合わせると、GET は「Unsupported SNMP security
  level」で失敗し、断った要求は記録にも残らない（実測）。NetBelt が方式を
  弱めたことは GET の失敗でも分かるが、届いた要求の securityLevel でも
  じかに確かめる。
- send_v3_traps: pysnmp 7 の send_notification で v3 Trap を送る。これも
  専用のスレッドと専用のイベントループで動かし、呼び出し元（Qt の
  メインスレッド）のループには触らない。
- SilentUdpPort: bind するだけで読まない UDP ソケット（応答しない機器）。
- ManagerRecorder: SNMPManager のシグナルを記録し、操作が終わるまで
  Qt のイベントを回す（NetBelt へはシグナルと公開のメソッドでだけ触る）。

値の OID は RFC 5612 の文書用の企業番号 32473 の下に置き、アドレスは
127.0.0.1 と RFC 5737 の 192.0.2.1 だけを使う。
"""
import asyncio
import bisect
import itertools
import socket
import threading
import time

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.hlapi.v3arch.asyncio import (
    USM_AUTH_HMAC96_MD5, USM_AUTH_HMAC96_SHA, USM_AUTH_HMAC128_SHA224,
    USM_AUTH_HMAC192_SHA256, USM_AUTH_HMAC256_SHA384, USM_AUTH_HMAC384_SHA512,
    USM_AUTH_NONE, USM_PRIV_CBC56_DES, USM_PRIV_CBC168_3DES,
    USM_PRIV_CFB128_AES, USM_PRIV_CFB192_AES, USM_PRIV_CFB192_AES_BLUMENTHAL,
    USM_PRIV_CFB256_AES, USM_PRIV_CFB256_AES_BLUMENTHAL, USM_PRIV_NONE,
    ContextData, NotificationType, ObjectIdentity, ObjectType, SnmpEngine,
    UdpTransportTarget, UsmUserData, send_notification)
from pysnmp.proto import rfc1902, rfc1905
from pysnmp.smi import instrum

HOST = "127.0.0.1"

# RFC 5612 の文書用の企業番号
ENTERPRISE = (1, 3, 6, 1, 4, 1, 32473)
# 型ごとの値を 1 つずつ置く部分木
TYPED_SUBTREE = ENTERPRISE + (1,)
# TYPED_SUBTREE のすぐ外（WALK が拾ってはいけない隣の値）
OUTSIDE_OID = ENTERPRISE + (2, 0)
# 100 件ごとの進捗を見るための大きな部分木
LARGE_SUBTREE = ENTERPRISE + (3,)
LARGE_ROWS = 250
# MIB の最後の部分木（この後ろは endOfMibView）
LAST_SUBTREE = ENTERPRISE + (4,)

# Trap に載せる目印の varbind（sysName.0）
TRAP_LABEL_OID = (1, 3, 6, 1, 2, 1, 1, 5, 0)
# coldStart
TRAP_OID = (1, 3, 6, 1, 6, 3, 1, 1, 5, 1)

# UI の選択肢の名前 → pysnmp の定数。NetBelt の resolve_v3_protocols を
# 使うと、NetBelt の表の取り違えを NetBelt の表で確かめることになるので、
# テスト側で独立に持つ（pysnmp の定義の名前から引く）。
AUTH_PROTOCOLS = {
    "none": USM_AUTH_NONE,
    "MD5": USM_AUTH_HMAC96_MD5,
    "SHA": USM_AUTH_HMAC96_SHA,
    "SHA-224": USM_AUTH_HMAC128_SHA224,
    "SHA-256": USM_AUTH_HMAC192_SHA256,
    "SHA-384": USM_AUTH_HMAC256_SHA384,
    "SHA-512": USM_AUTH_HMAC384_SHA512,
}
# AES-192/256 は Reeder 版（Cisco 等と相互接続する方。NetBelt の既定）
PRIV_PROTOCOLS = {
    "none": USM_PRIV_NONE,
    "DES": USM_PRIV_CBC56_DES,
    "3DES": USM_PRIV_CBC168_3DES,
    "AES-128": USM_PRIV_CFB128_AES,
    "AES-192": USM_PRIV_CFB192_AES,
    "AES-256": USM_PRIV_CFB256_AES,
}
# 取り違えの確認に使う Blumenthal 版
BLUMENTHAL_PRIV_PROTOCOLS = {
    "AES-192": USM_PRIV_CFB192_AES_BLUMENTHAL,
    "AES-256": USM_PRIV_CFB256_AES_BLUMENTHAL,
}


def oid_text(oid):
    """数字の組の OID を「1.3.6...」の文字列にする"""
    return ".".join(str(part) for part in oid)


def v3_user(username, auth="none", priv="none", auth_password=None,
            priv_password=None, auth_protocol=None, priv_protocol=None):
    """テスト用の v3 ユーザ定義（相手役に渡す形）を作る。

    auth / priv は UI の名前。auth_protocol / priv_protocol を渡すと、
    名前の表を通さずにその定数を使う（Blumenthal 版を登録するとき）。
    パスワードは明らかな架空値で、ユーザごとに変える（ほかのユーザの鍵で
    たまたま通ってしまう取り違えを拾うため）。
    """
    return {
        "username": username,
        "auth": auth,
        "priv": priv,
        "auth_protocol": (AUTH_PROTOCOLS[auth] if auth_protocol is None
                          else auth_protocol),
        "priv_protocol": (PRIV_PROTOCOLS[priv] if priv_protocol is None
                          else priv_protocol),
        "auth_password": ((auth_password or "auth-pass-" + username)
                          if auth != "none" else ""),
        "priv_password": ((priv_password or "priv-pass-" + username)
                          if priv != "none" else ""),
    }


def netbelt_v3_params(user):
    """v3_user の定義を、SNMPManager.snmp_get / snmp_walk の引数にする"""
    return {
        "version": "v3",
        "username": user["username"],
        "auth_protocol": user["auth"],
        "auth_password": user["auth_password"],
        "priv_protocol": user["priv"],
        "priv_password": user["priv_password"],
    }


def netbelt_trap_user(user, engine_ids):
    """v3_user の定義を、SNMPTrapReceiver の v3_users の 1 要素にする"""
    return {
        "username": user["username"],
        "auth_protocol": user["auth"],
        "auth_password": user["auth_password"],
        "priv_protocol": user["priv"],
        "priv_password": user["priv_password"],
        "engine_ids": list(engine_ids),
    }


def free_udp_port():
    """空いている UDP ポートの番号を 1 つ調べる（0 で bind して閉じる）。

    Trap の受信側は NetBelt 自身が bind するので、番号を先に決めて渡す。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def security_level(auth, priv):
    """UI の名前の組から、pysnmp が報告する securityLevel の数字（文字列）"""
    if priv != "none":
        return "3"
    if auth != "none":
        return "2"
    return "1"


# 相手役ごとに別の engineID を使う。USM は engineID ごとに時刻と鍵を
# 持つので、使い回すと前のテストの状態を引きずりうる。
# 0x80007ed9 = 先頭ビット + 企業番号 32473、04 = 以降は文字列
_engine_serial = itertools.count(1)


def new_engine_id(role):
    """テスト用の engineID（16 進文字列）を作る"""
    text = "netbelt-%s-%d" % (role, next(_engine_serial))
    return "80007ed904" + text.encode("ascii").hex()


def default_values():
    """相手役が返す値。型ごとの値・部分木の外の隣・大きな部分木・最後の部分木"""
    values = {
        TYPED_SUBTREE + (1, 0): rfc1902.OctetString(b"netbelt-agent"),
        # 非 ASCII（UTF-8 のバイト列）。prettyPrint は 16 進になる
        TYPED_SUBTREE + (2, 0): rfc1902.OctetString(
            "ネットベルト".encode("utf-8")),
        # 印字できないバイト（RFC 7042 の文書用 MAC）
        TYPED_SUBTREE + (3, 0): rfc1902.OctetString(
            bytes.fromhex("00005e005301")),
        TYPED_SUBTREE + (4, 0): rfc1902.Integer32(-42),
        TYPED_SUBTREE + (5, 0): rfc1902.Counter32(4000000000),
        TYPED_SUBTREE + (6, 0): rfc1902.Gauge32(1000000),
        TYPED_SUBTREE + (7, 0): rfc1902.TimeTicks(8640000),
        TYPED_SUBTREE + (8, 0): rfc1902.IpAddress("192.0.2.1"),
        TYPED_SUBTREE + (9, 0): rfc1902.ObjectIdentifier(ENTERPRISE + (99, 1)),
        # v1 では運べない（応答側が GET は noSuchName、GETNEXT は読み飛ばす）
        TYPED_SUBTREE + (10, 0): rfc1902.Counter64(18446744073709551615),
        OUTSIDE_OID: rfc1902.OctetString(b"outside"),
        LAST_SUBTREE + (1, 0): rfc1902.Integer32(1),
        LAST_SUBTREE + (2, 0): rfc1902.OctetString(b"last"),
    }
    for index in range(1, LARGE_ROWS + 1):
        values[LARGE_SUBTREE + (index, 0)] = rfc1902.OctetString(
            "row-%d" % index)
    return values


class _Instrumentation(instrum.AbstractMibInstrumController):
    """dict の値を返す MIB 計装（GET と GETNEXT だけ）"""

    def __init__(self, agent):
        self._agent = agent

    def read_variables(self, *var_binds, **ctx):
        return [self._agent._read(name) for name, _value in var_binds]

    def read_next_variables(self, *var_binds, **ctx):
        return [self._agent._read_next(name) for name, _value in var_binds]


class _InterceptingUdpTransport(udp.UdpAsyncioTransport):
    """受け取ったデータグラムを、相手役の指示で保留・破棄できる UDP"""

    def __init__(self, agent, loop):
        super().__init__(loop=loop)
        self._agent = agent

    def datagram_received(self, datagram, transportAddress):
        if self._agent._intercept(self, datagram, transportAddress):
            return
        super().datagram_received(datagram, transportAddress)


def _close_loop(loop):
    """残ったタスクを片付けてからループを閉じる（警告を残さない）"""
    try:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        # 閉じたソケットの後始末（proactor の完了通知）を受け取る
        loop.run_until_complete(asyncio.sleep(0.05))
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()


class LoopbackAgent:
    """127.0.0.1 の空きポートで GET / GETNEXT / GETBULK に答える相手役。

    使い方:
        agent = LoopbackAgent(v3_users=[v3_user("nb-sha", "SHA")])
        port = agent.start()
        ...
        agent.stop()

    served は「応答に載せた値の数」（同じ OID の再送分は数えない）。
    hold_after(n) は、n 個の値を返したあとに届いた要求を保留し、release()
    で処理する（応答を待っている間の取り消しを作る）。drop_after(n) は、
    n 個返したあとに届いた要求を捨て続ける（途中で応答をやめた機器）。
    """

    def __init__(self, values=None, communities=("public",), v3_users=()):
        self.values = dict(default_values() if values is None else values)
        self._keys = sorted(self.values)
        self._parents = {key[:-1] for key in self._keys}
        self.communities = list(communities)
        self.v3_users = list(v3_users)
        self.engine_id = new_engine_id("agent")
        self.port = None
        self.held = threading.Event()
        self._lock = threading.Lock()
        self._served = set()
        # 受けた要求の (securityModel, securityName, securityLevel)
        self._requests = []
        self._hold_at = None
        self._drop_at = None
        self._held = []
        self._ready = threading.Event()
        self._error = None
        self._loop = None
        self._thread = None
        # start() が起動を待ちきれずに諦めたら立てる（_run はループを回さない）
        self._abandoned = False

    # --- テストから触る口 ---

    @property
    def served(self):
        with self._lock:
            return len(self._served)

    def security_levels(self, username):
        """username の v3（USM）要求が届いたときの securityLevel の集合。

        1=noAuthNoPriv, 2=authNoPriv, 3=authPriv。ユーザ名で絞るので、
        engineID を調べる最初の要求（ユーザ名が空）は入らない。
        """
        with self._lock:
            return {level for model, name, level in self._requests
                    if model == 3 and name == username}

    def hold_after(self, rows):
        with self._lock:
            self._hold_at = rows

    def drop_after(self, rows):
        with self._lock:
            self._drop_at = rows

    def release(self):
        """保留をやめ、保留していた要求を届いた順に処理する"""
        with self._lock:
            self._hold_at = None
            held, self._held = self._held, []

        def redeliver():
            for transport, datagram, address in held:
                udp.UdpAsyncioTransport.datagram_received(
                    transport, datagram, address)

        self._loop.call_soon_threadsafe(redeliver)

    def start(self, timeout=15):
        self._thread = threading.Thread(
            target=self._run, name="snmp-loopback-agent", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            # 遅れて起動した応答側が、ソケットとループを持ったまま回り続けない
            # ようにする（呼び出し側は start() が成功してから後始末を登録する）
            self._abandoned = True
            try:
                self.stop(timeout)
            except RuntimeError:
                pass
            raise RuntimeError("テスト用の SNMP 応答側が %d 秒で起動しない" % timeout)
        if self._error is not None:
            raise self._error
        return self.port

    def stop(self, timeout=15):
        """ループを止めてソケットを閉じ、スレッドの終わりを待つ（何度呼んでもよい）"""
        thread = self._thread
        if thread is None:
            return
        if thread.is_alive() and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass    # 既に閉じたループ
        thread.join(timeout)
        if thread.is_alive():
            raise RuntimeError("テスト用の SNMP 応答側のスレッドが止まらない")
        self._thread = None

    # --- 応答側のスレッドの中で動く部分 ---

    def _read(self, name):
        oid = tuple(name)
        if oid in self.values:
            with self._lock:
                self._served.add(oid)
            return rfc1902.ObjectName(oid), self.values[oid]
        # 親（スカラ）がある OID はインスタンスだけが無い
        if oid[:-1] in self._parents:
            return rfc1902.ObjectName(oid), rfc1905.noSuchInstance
        return rfc1902.ObjectName(oid), rfc1905.noSuchObject

    def _read_next(self, name):
        oid = tuple(name)
        index = bisect.bisect_right(self._keys, oid)
        if index >= len(self._keys):
            return rfc1902.ObjectName(oid), rfc1905.endOfMibView
        key = self._keys[index]
        with self._lock:
            self._served.add(key)
        return rfc1902.ObjectName(key), self.values[key]

    def _observe_request(self, snmp_engine, execpoint, variables, cb_ctx):
        """応答側へ渡る要求のセキュリティ情報を記録する（observer）"""
        record = (int(variables.get("securityModel") or 0),
                  str(variables.get("securityName") or ""),
                  int(variables.get("securityLevel") or 0))
        with self._lock:
            self._requests.append(record)

    def _intercept(self, transport, datagram, address):
        with self._lock:
            served = len(self._served)
            if self._drop_at is not None and served >= self._drop_at:
                return True
            if self._hold_at is not None and served >= self._hold_at:
                self._held.append((transport, datagram, address))
                self.held.set()
                return True
        return False

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        snmp_engine = None
        try:
            snmp_engine = engine.SnmpEngine(
                snmpEngineID=rfc1902.OctetString(hexValue=self.engine_id))
            # 7.1.28 の pysnmp/proto/rfc3412.py の receive_message が、応答側へ
            # 渡す直前に 'rfc3412.receiveMessage:request' で呼ぶ（536 行目）
            snmp_engine.observer.register_observer(
                self._observe_request, "rfc3412.receiveMessage:request")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((HOST, 0))
                self.port = sock.getsockname()[1]
                transport = _InterceptingUdpTransport(self, loop)
                config.add_transport(snmp_engine, udp.DOMAIN_NAME,
                                     transport.open_server_mode(sock=sock))
            except BaseException:
                sock.close()
                raise
            for index, community in enumerate(self.communities):
                config.add_v1_system(snmp_engine, "agent-%d" % index, community)
            for user in self.v3_users:
                config.add_v3_user(
                    snmp_engine, user["username"],
                    user["auth_protocol"], user["auth_password"] or None,
                    user["priv_protocol"], user["priv_password"] or None)
            snmp_context = context.SnmpContext(snmp_engine)
            # 既定のコンテキストの計装を、dict の値を返すものに差し替える
            snmp_context.unregister_context_name(b"")
            snmp_context.register_context_name(b"", _Instrumentation(self))
            for responder in (cmdrsp.GetCommandResponder,
                              cmdrsp.NextCommandResponder,
                              cmdrsp.BulkCommandResponder):
                responder(snmp_engine, snmp_context)
            # 待ち受けのソケットができるまで回す（ここまでに届いた要求は
            # OS のバッファに残るが、準備ができてから「起動した」と返す）
            loop.run_until_complete(asyncio.sleep(0.05))
        except BaseException as e:
            self._error = e
            if snmp_engine is not None:
                try:
                    snmp_engine.close_dispatcher()
                except Exception:
                    pass
            _close_loop(loop)
            asyncio.set_event_loop(None)
            self._ready.set()
            return
        self._ready.set()
        try:
            # start() が待ちきれずに諦めていたら回さない（stop() がまだループを
            # 知らないうちに諦めた場合の分。知っていれば loop.stop が積まれている）
            if not self._abandoned:
                loop.run_forever()
        finally:
            try:
                snmp_engine.close_dispatcher()
            finally:
                _close_loop(loop)
                asyncio.set_event_loop(None)


class SilentUdpPort:
    """bind するだけで何も読まない UDP ポート（応答しない機器の代わり）。

    bind していれば ICMP の到達不能は返らないので、送った側は応答待ちの
    タイムアウトまで待つことになる。
    """

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((HOST, 0))
        self.port = self._sock.getsockname()[1]

    def close(self):
        self._sock.close()


def _usm_user_data(user):
    """v3_user の定義から pysnmp の UsmUserData を作る（方式は必ず明示する）"""
    if user["auth"] == "none" and user["auth_protocol"] == USM_AUTH_NONE:
        return UsmUserData(user["username"])
    if user["priv_protocol"] == USM_PRIV_NONE:
        return UsmUserData(user["username"], user["auth_password"],
                           authProtocol=user["auth_protocol"])
    return UsmUserData(user["username"], user["auth_password"],
                       user["priv_password"],
                       authProtocol=user["auth_protocol"],
                       privProtocol=user["priv_protocol"])


def run_in_own_loop(coroutine_factory, timeout=60):
    """専用のスレッドと専用のイベントループでコルーチンを 1 つ動かし、結果を返す"""
    outcome = {}

    def target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            task = loop.create_task(coroutine_factory())
            outcome["loop"], outcome["task"] = loop, task
            outcome["value"] = loop.run_until_complete(task)
        except BaseException as e:
            outcome["error"] = e
        finally:
            _close_loop(loop)
            asyncio.set_event_loop(None)

    thread = threading.Thread(target=target, name="snmp-test-loop", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        # 失敗したテストのあとに、遅れて Trap を送る処理を残さない。
        # 取り消して、スレッド（ループの後始末を含む）の終わりを待つ
        loop, task = outcome.get("loop"), outcome.get("task")
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass    # 既に閉じたループ
        thread.join(15)
        raise RuntimeError("テスト用の SNMP の処理が %d 秒で終わらない" % timeout)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def send_v3_traps(port, engine_id, notifications, timeout=60):
    """v3 Trap を順に送る。

    Args:
        port: 送り先（127.0.0.1）のポート
        engine_id: 送信側の engineID（16 進文字列）。Trap では送信側が
            authoritative なので、受信側はこれを登録しておく必要がある
        notifications: (v3_user の定義, 目印の文字列) の並び

    Returns:
        [(目印, 送信側のエラー or None), ...]。送信側で暗号化できなかった
        など、そもそも送れなかったものはエラーの文字列が入る

    同じソケットから届いた順に送るので、最後に送った Trap が届いたら、
    それより前に送った Trap は受信側で処理済みになっている。
    """
    async def send_all():
        results = []
        snmp_engine = SnmpEngine(
            snmpEngineID=rfc1902.OctetString(hexValue=engine_id))
        try:
            for user, label in notifications:
                # 宛先は Trap ごとに作り直す。pysnmp の LCD は、初めて使った
                # 宛先の tagList を最初のユーザ名から決めて書き換える。同じ宛先を
                # 使い回すと、それまでに登録した全ユーザの宛先が同じタグに載り、
                # 1 回送るたびに全員分の Trap が出る（実測）
                target = await UdpTransportTarget.create(
                    (HOST, port), timeout=1, retries=0)
                try:
                    error_indication, _status, _index, _binds = (
                        await send_notification(
                            snmp_engine, _usm_user_data(user), target,
                            ContextData(), "trap",
                            NotificationType(ObjectIdentity(oid_text(TRAP_OID)))
                            .add_varbinds(ObjectType(
                                ObjectIdentity(oid_text(TRAP_LABEL_OID)),
                                rfc1902.OctetString(label)))))
                except Exception as e:
                    results.append((label, "%s: %s" % (type(e).__name__, e)))
                else:
                    results.append(
                        (label, str(error_indication) if error_indication else None))
            # 送信用のソケットができて、積んだ Trap が OS へ渡るまで待つ
            # （ソケットは最初の送信のときに非同期で作られる）
            transport = config.get_transport(snmp_engine, udp.DOMAIN_NAME)
            deadline = time.monotonic() + 10
            while (transport is not None and transport.transport is None
                   and time.monotonic() < deadline):
                await asyncio.sleep(0.01)
            if transport is None or transport.transport is None:
                # 送れていないのに戻ると、「届かないこと」を確かめるテストが
                # 何も送らずに通ってしまう
                raise RuntimeError("Trap を送るソケットが 10 秒で用意できない")
            await asyncio.sleep(0.1)
        finally:
            snmp_engine.close_dispatcher()
        return results

    return run_in_own_loop(send_all, timeout)


# --- NetBelt の SNMPManager を動かす側 ---

def pump_until(condition, timeout):
    """condition() が真になるまで Qt のイベントを回す。なれば True"""
    from PyQt6.QtWidgets import QApplication

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return condition()


class ManagerRecorder:
    """SNMPManager のシグナルを記録し、GET/WALK を 1 回ずつ最後まで動かす"""

    def __init__(self, manager):
        self.manager = manager
        self.completed = []
        self.partial = []
        self.cancelled = []
        self.progress = []
        self.errors = []
        manager.operation_completed.connect(
            lambda ok, result: self.completed.append((ok, result)))
        manager.operation_partial.connect(self.partial.append)
        manager.operation_cancelled.connect(self.cancelled.append)
        manager.progress_update.connect(self.progress.append)
        manager.error_occurred.connect(self.errors.append)

    def reset(self):
        for records in (self.completed, self.partial, self.cancelled,
                        self.progress, self.errors):
            del records[:]

    def start(self, start):
        """要求を出す（受け付けられなければ AssertionError）"""
        self.reset()
        if not start():
            raise AssertionError(
                "SNMPManager が要求を受け付けない: %r" % (self.errors,))

    def wait(self, timeout=60):
        """ワーカーが終わって参照が外れるまで待つ（その時点で結果は配送済み）"""
        if not pump_until(lambda: self.manager.worker is None, timeout):
            # 失敗したテストのあとに、WALK を回し続けるワーカーを残さない。
            # 取り消しは次の応答で効く（応答しない相手なら既定の待ちの約 6 秒）
            self.manager.request_cancel()
            pump_until(lambda: self.manager.worker is None, 15)
            raise AssertionError("SNMP の操作が %d 秒で終わらない" % timeout)

    def run(self, start, timeout=60):
        """要求を出して終わるまで待ち、かかった秒数を返す"""
        began = time.monotonic()
        self.start(start)
        self.wait(timeout)
        return time.monotonic() - began

    def result(self):
        """operation_completed がちょうど 1 回出たことを確かめ、(ok, 結果) を返す"""
        if len(self.completed) != 1:
            raise AssertionError(
                "operation_completed が %d 回（1 回のはず）: completed=%r "
                "cancelled=%r errors=%r" % (len(self.completed), self.completed,
                                           self.cancelled, self.errors))
        return self.completed[0]
