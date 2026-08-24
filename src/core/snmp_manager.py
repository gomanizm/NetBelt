"""
SNMPマネージャー

GET/WALK/Trap受信をサポートし、SNMPv1/v2c/v3に対応
"""
from PyQt6.QtCore import QObject, pyqtSignal, QThread
from typing import List, Dict, Optional, Tuple
import threading
from datetime import datetime
from .sockets import set_exclusive_bind

# pysnmp 5.x用のインポート (pyasn1バージョンによっては使用不可)
try:
    from pysnmp.hlapi import *
    from pysnmp.entity import engine, config
    from pysnmp.carrier.asyncore.dgram import udp
    from pysnmp.entity.rfc3413 import ntfrcv
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
# ため、値は表示にもファイルにも載せない。
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

    # SHA-2 系の定数名は「HMAC<出力ビット長>SHA<ダイジェスト長>」の順であり、
    # SHA-256 は usmHMAC192SHA256AuthProtocol になる（usmHMACSHA256... ではない）。
    auth_table = {
        "none": usmNoAuthProtocol,
        "MD5": usmHMACMD5AuthProtocol,
        "SHA": usmHMACSHAAuthProtocol,
        "SHA-224": usmHMAC128SHA224AuthProtocol,
        "SHA-256": usmHMAC192SHA256AuthProtocol,
        "SHA-384": usmHMAC256SHA384AuthProtocol,
        "SHA-512": usmHMAC384SHA512AuthProtocol,
    }
    # AES-192/256 は Reeder 版（名前が短い方）を使う。pysnmp のソースが
    # 「non-standard but used by many vendors」と書いている方で、Cisco 等の
    # 実装と相互接続するのはこちら。Blumenthal 版は名前に Blumenthal が入る。
    priv_table = {
        "none": usmNoPrivProtocol,
        "DES": usmDESPrivProtocol,
        "3DES": usm3DESEDEPrivProtocol,
        "AES-128": usmAesCfb128Protocol,
        "AES-192": usmAesCfb192Protocol,
        "AES-256": usmAesCfb256Protocol,
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


class SNMPWorker(QThread):
    """SNMP操作を別スレッドで実行するワーカー"""
    
    # シグナル定義
    result_ready = pyqtSignal(bool, object)  # (success, result)
    progress_update = pyqtSignal(str)  # ステータスメッセージ
    
    def __init__(self, operation: str, params: dict):
        super().__init__()
        self.operation = operation
        self.params = params
        self._cancelled = False
    
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
            
            if not self._cancelled:
                self.result_ready.emit(True, result)
        
        except Exception as e:
            if not self._cancelled:
                self.result_ready.emit(False, str(e))
    
    def cancel(self):
        """操作をキャンセル"""
        self._cancelled = True
    
    def _perform_get(self) -> List[Tuple[str, str, str]]:
        """SNMP GETを実行"""
        host = self.params['host']
        port = self.params.get('port', 161)
        oids = self.params['oids']  # リスト
        version = self.params.get('version', 'v2c')
        
        # 認証情報の準備
        auth_data = self._prepare_auth_data(version)
        
        # ObjectIdentityのリストを作成
        object_identities = [ObjectType(ObjectIdentity(oid)) for oid in oids]
        
        # SNMP GET実行
        iterator = getCmd(
            SnmpEngine(),
            auth_data,
            UdpTransportTarget((host, port)),
            ContextData(),
            *object_identities
        )
        
        errorIndication, errorStatus, errorIndex, varBinds = next(iterator)
        
        if errorIndication:
            raise Exception(f"SNMP Error: {errorIndication}")
        elif errorStatus:
            raise Exception(f"SNMP Error: {errorStatus.prettyPrint()}")
        
        # 結果を整形 [(OID, Type, Value), ...]
        results = []
        for varBind in varBinds:
            oid = varBind[0].prettyPrint()
            value = varBind[1].prettyPrint()
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
        
        # SNMP WALK実行
        results = []
        count = 0
        
        for (errorIndication, errorStatus, errorIndex, varBinds) in nextCmd(
            SnmpEngine(),
            auth_data,
            UdpTransportTarget((host, port)),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False
        ):
            if self._cancelled:
                break
            
            if errorIndication:
                raise Exception(f"SNMP Error: {errorIndication}")
            elif errorStatus:
                raise Exception(f"SNMP Error: {errorStatus.prettyPrint()}")
            
            for varBind in varBinds:
                oid_str = varBind[0].prettyPrint()
                value = varBind[1].prettyPrint()
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
            # authKey だけ渡すと既定で MD5、privKey だけなら既定で DES を選ぶため。
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


class SNMPTrapReceiver(QThread):
    """
    SNMP Trap受信スレッド

    pysnmp のエンジン（SnmpEngine + ntfrcv + AsyncoreDispatcher）で受信する。
    v3 は scopedPDU が暗号化され得るため、生ソケットで BER デコードする方式では
    中身を取り出せない。USM の復号経路を持つエンジンに載せる必要がある。
    """

    # シグナル定義
    trap_received = pyqtSignal(dict)  # Trap情報
    error_occurred = pyqtSignal(str)  # エラーメッセージ
    started = pyqtSignal()  # 開始通知
    stopped = pyqtSignal()  # 停止通知

    # ディスパッチャを止めるときのジョブID（pysnmp の慣例で 1 を使う）
    _JOB_ID = 1

    def __init__(self, port: int = 162, communities: List[str] = None,
                 v3_users: List[dict] = None):
        """
        Args:
            port: 受信ポート
            communities: 許可する v1/v2c コミュニティ名のリスト
            v3_users: v3 ユーザの定義リスト。各要素は
                {"username", "auth_protocol", "auth_password",
                 "priv_protocol", "priv_password", "engine_ids"}
        """
        super().__init__()
        self.port = port
        # None（未指定）と []（v1/v2c を受けない）は別物。or で書くと
        # [] が既定値へ落ちるため、Trap のバージョンに v3 を選んで
        # パネルが [] を渡しても public の v1/v2c Trap が通ってしまう。
        self.communities = (['public'] if communities is None
                            else _clean_communities(communities))
        self.v3_users = list(v3_users or [])
        self._running = False
        self._engine = None
        self._transport = None
        # jobStarted と停止要求は別スレッドから触るのでロックで守る。
        # 先に jobFinished を呼ぶと pysnmp 内部で KeyError になり、
        # ジョブカウンタが不整合のまま runDispatcher() が戻らなくなる。
        self._state_lock = threading.Lock()
        self._job_started = False
        self._stop_requested = False
        # observer で拾った直近のセキュリティ情報（表示用の参考値）
        self._last_security = {}

    def bind(self) -> bool:
        """待ち受けを用意する。

        呼び出し元スレッドで実行し、失敗を戻り値で返す。スレッドの中で
        バインドすると成否を呼び出し側へ返せず、ポートが使用中でも UI は
        「受信中」の表示のまま何も待ち受けない状態になる。
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

        try:
            self._engine = engine.SnmpEngine()
            self._transport = udp.UdpTransport()

            # pysnmp は自前のソケットへ無条件に REUSEADDR 系のオプションを立てる。
            # Windows ではそれだけだと他プロセスの待ち受けポートを奪えてしまうので、
            # 排他バインドへ差し替える（set_exclusive_bind が REUSEADDR を戻す）。
            set_exclusive_bind(self._transport.socket)
            self._transport.openServerMode(('0.0.0.0', self.port))
            config.addTransport(self._engine, udp.domainName, self._transport)

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
            config.addV1System(self._engine, f"netbelt-v2c-{index}", community)
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
                config.addV3User(
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

        observer は pysnmp のディスパッチ経路の中で呼ばれ、ここで例外が
        出ると runDispatcher() を抜けて受信が完全に止まる。旧実装は
        受信ループの中で握っていたので1パケットで止まることは無かった。
        _on_notification と同じ扱いに揃える。
        """
        def _observe(snmp_engine, execpoint, variables, cb_ctx):
            try:
                self._capture_security(variables)
            except Exception as e:
                print(f"[SNMPTrapReceiver] セキュリティ情報を拾えません: {e}")

        self._engine.observer.registerObserver(
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

        ntfrcv はコールバックのアリティを例外ベースで判定し、TypeError が出ると
        「引数の数が違う」とみなして呼び直す。本体で TypeError を漏らすと
        同じ通知が二度処理されるため、ここで握りつぶす。
        """
        try:
            self.trap_received.emit(self._build_trap_data(var_binds))
        except TypeError as e:
            print(f"[SNMPTrapReceiver] 通知処理エラー: {e}")
        except Exception as e:
            print(f"[SNMPTrapReceiver] 通知処理エラー: {e}")

    def _build_trap_data(self, var_binds) -> dict:
        """
        受信した VarBinds を trap_data の形へ整える

        Args:
            var_binds: (ObjectName, ObjectSyntax) のシーケンス

        Returns:
            trap_received で emit する dict
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
        try:
            print(f"[SNMPTrapReceiver] 開始: ポート{self.port}")
            if self._engine is None and not self.bind():
                return

            # コミュニティ文字列と v3 のパスワードは実質的な認証情報。
            # 凍結ビルドでは stdout がログへ恒久保存されるため値は出さない。
            print(f"[SNMPTrapReceiver] 許可コミュニティ: {len(self.communities)}件")
            print(f"[SNMPTrapReceiver] v3ユーザ: {len(self.v3_users)}件")
            print(f"[SNMPTrapReceiver] Trap受信待機中...")

            self._running = True
            with self._state_lock:
                self._engine.transportDispatcher.jobStarted(self._JOB_ID)
                self._job_started = True
                # ここより前に来ていた停止要求は jobFinished を呼べていない
                stop_before_start = self._stop_requested
            self.started.emit()

            if stop_before_start:
                self._engine.transportDispatcher.jobFinished(self._JOB_ID)

            self._engine.transportDispatcher.runDispatcher()

            print(f"[SNMPTrapReceiver] 正常終了")

        except Exception as e:
            print(f"[SNMPTrapReceiver] エラー: {str(e)}")
            import traceback
            traceback.print_exc()
            self.error_occurred.emit(f"Trap受信エラー: {str(e)}")

        finally:
            self._running = False
            self._close_engine()
            # 同じ受信機をもう一度 start() したとき、前回の停止要求が
            # 残っていると jobStarted の直後に jobFinished して即終了する。
            with self._state_lock:
                self._job_started = False
                self._stop_requested = False
            # 例外で抜けたときも必ず知らせる。ここを成功経路だけに
            # 置くと、受信が死んでも画面は「受信中」のまま残る。
            self.stopped.emit()

    def _close_engine(self):
        """エンジンとトランスポートを片付ける"""
        if self._engine is not None:
            try:
                self._engine.transportDispatcher.closeDispatcher()
            except Exception:
                pass
        self._engine = None
        self._transport = None

    def stop(self):
        """Trap受信を停止

        jobFinished でディスパッチャのジョブを終わらせると runDispatcher() が
        戻る。検知はディスパッチャのタイマ分解能（0.5秒）の周期。

        スレッドが jobStarted に到達する前に呼ばれた場合は、ここでは何もせず
        run() 側に終わらせてもらう。先回りして jobFinished を呼ぶと
        pysnmp 内部で KeyError になり、ジョブカウンタが合わなくなって
        runDispatcher() が永久に戻らなくなる。
        """
        print(f"[SNMPTrapReceiver] 停止要求")
        self._running = False

        with self._state_lock:
            self._stop_requested = True
            if not self._job_started:
                return

        if self._engine is not None:
            try:
                self._engine.transportDispatcher.jobFinished(self._JOB_ID)
            except Exception:
                pass


class SNMPManager(QObject):
    """SNMPマネージャー"""
    
    # シグナル定義
    operation_started = pyqtSignal(str)  # 操作名
    operation_completed = pyqtSignal(bool, object)  # (success, result)
    progress_update = pyqtSignal(str)  # ステータスメッセージ
    error_occurred = pyqtSignal(str)  # エラーメッセージ
    trap_received = pyqtSignal(dict)  # Trap受信
    trap_receiver_started = pyqtSignal()  # Trap受信開始
    trap_receiver_stopped = pyqtSignal()  # Trap受信停止
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.trap_receiver = None
        # 停止しきらなかったスレッドを保持する。参照を落とすと実行中の QThread が
        # 破棄されてプロセスごと落ちるため、終わるまで手放さない。
        self._retired_receivers = []
    
    def snmp_get(self, host: str, oids: List[str], **kwargs):
        """
        SNMP GET操作を実行
        
        Args:
            host: ホストアドレス
            oids: OIDのリスト
            **kwargs: その他のパラメータ (port, version, community, など)
        """
        if self.worker and self.worker.isRunning():
            self.error_occurred.emit("既に操作が実行中です")
            return
        
        params = {
            'host': host,
            'oids': oids,
            **kwargs
        }
        
        self.worker = SNMPWorker('get', params)
        self.worker.result_ready.connect(self._on_operation_completed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.progress_update.connect(self.progress_update.emit)
        self.worker.start()
        
        self.operation_started.emit(f"SNMP GET: {host}")
    
    def snmp_walk(self, host: str, oid: str, **kwargs):
        """
        SNMP WALK操作を実行
        
        Args:
            host: ホストアドレス
            oid: 開始OID
            **kwargs: その他のパラメータ (port, version, community, など)
        """
        if self.worker and self.worker.isRunning():
            self.error_occurred.emit("既に操作が実行中です")
            return
        
        params = {
            'host': host,
            'oid': oid,
            **kwargs
        }
        
        self.worker = SNMPWorker('walk', params)
        self.worker.result_ready.connect(self._on_operation_completed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.progress_update.connect(self.progress_update.emit)
        self.worker.start()
        
        self.operation_started.emit(f"SNMP WALK: {host} - {oid}")
    
    def cancel_operation(self):
        """現在の操作をキャンセル"""
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
    
    def start_trap_receiver(self, port: int = 162, communities: List[str] = None,
                            v3_users: List[dict] = None):
        """
        SNMP Trap受信を開始
        
        Args:
            port: 受信ポート (デフォルト: 162)
            communities: 許可するコミュニティ名のリスト
            v3_users: v3 ユーザの定義リスト（SNMPTrapReceiver の docstring 参照）
        """
        if self.trap_receiver and self.trap_receiver.isRunning():
            self.error_occurred.emit("既にTrap受信が実行中です")
            return False
        
        # 受信ポートの Windows ファイアウォール受信許可を用意（Windowsのみ・冪等・必要時UAC）
        try:
            from .firewall import ensure_inbound_allow
            _ok, _msg = ensure_inbound_allow("SNMP Trap", "UDP", port)
            print(f"[SNMP] ファイアウォール: {_msg}")
        except Exception as _e:
            print(f"[SNMP] ファイアウォール設定エラー: {_e}")
        self.trap_receiver = SNMPTrapReceiver(port, communities, v3_users)
        self.trap_receiver.trap_received.connect(self.trap_received.emit)
        self.trap_receiver.error_occurred.connect(self.error_occurred.emit)
        self.trap_receiver.started.connect(self.trap_receiver_started.emit)
        self.trap_receiver.stopped.connect(self.trap_receiver_stopped.emit)
        # スレッドを起こす前にバインドし、失敗ならここで打ち切る
        if not self.trap_receiver.bind():
            self.trap_receiver = None
            return False
        self.trap_receiver.start()
        
        self.operation_started.emit(f"SNMP Trap受信開始: ポート{port}")
        return True
    
    def stop_trap_receiver(self):
        """SNMP Trap受信を停止

        isRunning() で分岐しないのは、スレッドが既に自分で終わっている場合にも
        参照を片付ける必要があるため（放置すると次回の起動判定が狂う）。
        """
        receiver = self.trap_receiver
        if receiver is None:
            return

        receiver.stop()
        finished = receiver.wait(5000)  # 受信ループは最大1秒で停止を検知する
        if not finished:
            # terminate() は任意の位置でスレッドを殺すためソケットや内部状態が
            # 壊れる。ここでは強制終了せず、参照を保持して破棄だけ防ぐ
            # （実行中の QThread を破棄するとプロセスごと落ちる）。
            print("[SNMPManager] 警告: Trap受信スレッドが5秒以内に終了しませんでした。"
                  "強制終了はせず、終了するまで参照を保持します")
            self._retire(receiver)

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

    def _forget(self, receiver):
        """終わった受信スレッドを保持リストから外す"""
        if receiver in self._retired_receivers:
            self._retired_receivers.remove(receiver)
    
    def is_trap_receiver_running(self) -> bool:
        """Trap受信が実行中かどうか"""
        return self.trap_receiver is not None and self.trap_receiver.isRunning()