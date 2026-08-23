"""
SNMPマネージャー

GET/WALK/Trap受信をサポートし、SNMPv1/v2c/v3に対応
"""
from PyQt6.QtCore import QObject, pyqtSignal, QThread
from typing import List, Dict, Optional, Tuple
import socket
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
            
            # 認証プロトコルの選択
            if auth_protocol == 'MD5':
                auth_proto = usmHMACMD5AuthProtocol
            elif auth_protocol == 'SHA':
                auth_proto = usmHMACSHAAuthProtocol
            else:
                auth_proto = usmNoAuthProtocol
            
            # 暗号化プロトコルの選択
            if priv_protocol == 'DES':
                priv_proto = usmDESPrivProtocol
            elif priv_protocol == 'AES':
                priv_proto = usmAesCfb128Protocol
            else:
                priv_proto = usmNoPrivProtocol
            
            # 認証データ作成
            if auth_protocol == 'none':
                return UsmUserData(username)
            elif priv_protocol == 'none':
                return UsmUserData(username, auth_password, authProtocol=auth_proto)
            else:
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
    
    Python標準のUDPソケットでパケットを受信し、pyasn1/pysnmpでデコードする実装
    """
    
    # シグナル定義
    trap_received = pyqtSignal(dict)  # Trap情報
    error_occurred = pyqtSignal(str)  # エラーメッセージ
    started = pyqtSignal()  # 開始通知
    stopped = pyqtSignal()  # 停止通知
    
    def __init__(self, port: int = 162, communities: List[str] = None):
        super().__init__()
        self.port = port
        self.communities = communities or ['public']
        self._running = False
        self._socket = None
    
    def _is_allowed_community(self, community: str) -> bool:
        """受信した Trap のコミュニティが許可一覧に含まれるか。

        SNMPv1/v2c のコミュニティは平文で流れるため強固な認証ではないが、
        誤送信や別システムの Trap を弾く実用的な効果がある。
        SNMP の仕様どおり大文字小文字は区別する。
        """
        return community in self.communities

    def bind(self) -> bool:
        """待ち受けソケットを用意する。

        呼び出し元スレッドで実行し、失敗を戻り値で返す。スレッドの中で
        バインドすると成否を呼び出し側へ返せず、ポートが使用中でも UI は
        「受信中」の表示のまま何も待ち受けない状態になる。
        """
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            set_exclusive_bind(self._socket)
            self._socket.bind(('0.0.0.0', self.port))
            self._socket.settimeout(1.0)  # 1秒タイムアウト
            return True
        except OSError as e:
            try:
                if self._socket:
                    self._socket.close()
            except Exception:
                pass
            self._socket = None
            self.error_occurred.emit(f"ポート {self.port} で待ち受けできません: {e}")
            return False

    def run(self):
        """Trap受信スレッドのメイン処理"""
        try:
            print(f"[SNMPTrapReceiver] 開始: ポート{self.port}")
            if self._socket is None and not self.bind():
                return
            
            print(f"[SNMPTrapReceiver] UDPソケット作成完了: 0.0.0.0:{self.port}")
            # コミュニティ文字列は SNMPv1/v2c の合言葉であり実質的な認証情報。
            # 凍結ビルドでは stdout が %LOCALAPPDATA% 配下のログへ恒久保存され、
            # 不具合報告への添付などで流出するため、値そのものは出さない。
            print(f"[SNMPTrapReceiver] 許可コミュニティ: {len(self.communities)}件")
            print(f"[SNMPTrapReceiver] Trap受信待機中...")
            
            self._running = True
            self.started.emit()
            
            while self._running:
                try:
                    # パケット受信
                    data, addr = self._socket.recvfrom(65535)
                    print(f"[SNMPTrapReceiver] ★★★ パケット受信 ★★★")
                    print(f"[SNMPTrapReceiver] 送信元: {addr[0]}:{addr[1]}")
                    print(f"[SNMPTrapReceiver] データ長: {len(data)} bytes")
                    
                    # pysnmpでSNMPパケットを解析
                    trap_data = self._parse_snmp_trap(data, addr)
                    
                    if trap_data:
                        # シグナル発行
                        self.trap_received.emit(trap_data)
                        print(f"[SNMPTrapReceiver] シグナル発行完了")
                    else:
                        print(f"[SNMPTrapReceiver] パケット解析失敗")
                
                except socket.timeout:
                    # タイムアウトは正常（継続）
                    continue
                except Exception as e:
                    if self._running:
                        print(f"[SNMPTrapReceiver] パケット処理エラー: {str(e)}")
                        import traceback
                        traceback.print_exc()
            
            print(f"[SNMPTrapReceiver] 正常終了")
            self.stopped.emit()
        
        except Exception as e:
            print(f"[SNMPTrapReceiver] エラー: {str(e)}")
            import traceback
            traceback.print_exc()
            self.error_occurred.emit(f"Trap受信エラー: {str(e)}")
        
        finally:
            # クリーンアップ
            if self._socket:
                try:
                    self._socket.close()
                except:
                    pass
    
    def stop(self):
        """Trap受信を停止"""
        print(f"[SNMPTrapReceiver] 停止要求")
        self._running = False
    
    def _parse_snmp_trap(self, data: bytes, addr: tuple) -> Optional[dict]:
        """
        SNMPパケットを解析
        
        pysnmpのデコーダーを使用して解析
        """
        try:
            from pyasn1.codec.ber import decoder
            from pysnmp.proto import api
            
            # SNMPバージョンを検出
            msgVer = api.decodeMessageVersion(data)
            if msgVer not in api.protoModules:
                print(f"[SNMPTrapReceiver] 未サポートSNMPバージョン: {msgVer}")
                return None
            
            pMod = api.protoModules[msgVer]
            
            # SNMPメッセージをデコード
            reqMsg, _ = decoder.decode(data, asn1Spec=pMod.Message())
            
            # コミュニティ取得（v1/v2c）と照合
            community = pMod.apiMessage.getCommunity(reqMsg)
            if not self._is_allowed_community(community.prettyPrint()):
                # 値そのものはログへ出さない（他システムの合言葉であり得るため）。
                # 送信元だけ残しておけば「なぜ届かないか」の切り分けには足りる。
                print(f"[SNMPTrapReceiver] 許可されていないコミュニティのため破棄: "
                      f"from {addr[0]}")
                return None
            
            # PDU取得
            reqPDU = pMod.apiMessage.getPDU(reqMsg)
            
            # Trap情報を整形
            trap_data = {
                'source_ip': addr[0],
                'source_port': addr[1],
                'timestamp': None,
                'trap_oid': None,
                'varbinds': [],
                'received_at': datetime.now().isoformat()
            }
            
            # VarBinds解析
            varBinds = pMod.apiPDU.getVarBinds(reqPDU)
            for oid, val in varBinds:
                oid_str = oid.prettyPrint()
                value_str = val.prettyPrint()
                value_type = val.__class__.__name__
                
                print(f"[SNMPTrapReceiver]   {oid_str} = {value_str} ({value_type})")
                
                # 特定OIDの処理
                if oid_str == '1.3.6.1.2.1.1.3.0':  # sysUpTime
                    trap_data['timestamp'] = value_str
                elif oid_str == '1.3.6.1.6.3.1.1.4.1.0':  # snmpTrapOID
                    trap_data['trap_oid'] = value_str
                
                trap_data['varbinds'].append({
                    'oid': oid_str,
                    'value': value_str,
                    'type': value_type
                })
            
            print(f"[SNMPTrapReceiver] Trap OID: {trap_data['trap_oid']}")
            return trap_data
        
        except Exception as e:
            print(f"[SNMPTrapReceiver] SNMP解析エラー: {str(e)}")
            import traceback
            traceback.print_exc()
            return None


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
    
    def start_trap_receiver(self, port: int = 162, communities: List[str] = None):
        """
        SNMP Trap受信を開始
        
        Args:
            port: 受信ポート (デフォルト: 162)
            communities: 許可するコミュニティ名のリスト
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
        self.trap_receiver = SNMPTrapReceiver(port, communities)
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
            self._retired_receivers.append(receiver)

        self.trap_receiver = None
        self.operation_started.emit("SNMP Trap受信停止")
    
    def is_trap_receiver_running(self) -> bool:
        """Trap受信が実行中かどうか"""
        return self.trap_receiver is not None and self.trap_receiver.isRunning()