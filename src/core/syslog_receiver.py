"""Syslogメッセージ受信サーバー"""
import socket
import threading
import re
from datetime import datetime
from PyQt6.QtCore import QObject, pyqtSignal
from typing import Dict, Optional
from .sockets import set_exclusive_bind


class SyslogMessage:
    """Syslogメッセージを表現するクラス"""
    
    # Syslog優先度レベル
    LEVELS = {
        0: "Emergency",
        1: "Alert",
        2: "Critical",
        3: "Error",
        4: "Warning",
        5: "Notice",
        6: "Info",
        7: "Debug"
    }
    
    def __init__(self, raw_message: str, source_ip: str, proto: str = None, port: int = None):
        """
        初期化
        
        Args:
            raw_message: 生のSyslogメッセージ
            source_ip: 送信元IPアドレス
            proto: 受信したプロトコル ("UDP"/"TCP")。不明なら None
            port: 受信した待受ポート。不明なら None
        """
        self.raw_message = raw_message
        self.source_ip = source_ip
        self.proto = proto
        self.port = port
        self.timestamp = datetime.now()
        self.priority = None
        self.facility = None
        self.severity = None
        self.level = "Info"
        self.hostname = source_ip
        self.message = raw_message
        
        # メッセージをパース
        self._parse()

    @property
    def source_display(self):
        """表示用の送信元（例: "192.0.2.1 (UDP/514)"）。proto/port 不明なら IP のみ"""
        if self.proto and self.port:
            return "%s (%s/%s)" % (self.source_ip, self.proto, self.port)
        return self.source_ip
    
    def _parse(self):
        """Syslogメッセージをパース"""
        try:
            # RFC 3164形式: <PRI>TIMESTAMP HOSTNAME MESSAGE
            # RFC 5424形式: <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA MSG
            
            # PRI部分を抽出
            pri_match = re.match(r'^<(\d+)>', self.raw_message)
            if pri_match:
                priority = int(pri_match.group(1))
                self.priority = priority
                self.facility = priority >> 3
                self.severity = priority & 0x07
                self.level = self.LEVELS.get(self.severity, "Info")
                
                # PRI以降のメッセージを取得
                message_after_pri = self.raw_message[pri_match.end():]
            else:
                # PRIがない場合はそのまま
                message_after_pri = self.raw_message
                self.level = "Info"
                self.severity = 6
            
            # RFC 5424形式チェック（VERSIONがある）
            version_match = re.match(r'^(\d+)\s+', message_after_pri)
            if version_match:
                # RFC 5424形式
                self._parse_rfc5424(message_after_pri)
            else:
                # RFC 3164形式
                self._parse_rfc3164(message_after_pri)
        
        except Exception as e:
            print(f"[Syslog] Parse error: {e}")
            # パースエラーの場合はそのまま表示
            self.message = self.raw_message
    
    def _parse_rfc3164(self, message: str):
        """RFC 3164形式のメッセージをパース"""
        try:
            # TIMESTAMP HOSTNAME MESSAGE の形式
            # 例: Jan  1 00:00:00 hostname message
            
            # タイムスタンプとホスト名を抽出（簡易版）
            parts = message.split(None, 3)
            if len(parts) >= 3:
                # parts[0]: Month, parts[1]: Day, parts[2]: Time, parts[3:]: Hostname + Message
                if len(parts) == 4:
                    # ホスト名とメッセージを分離
                    remaining = parts[3].split(None, 1)
                    if len(remaining) >= 1:
                        self.hostname = remaining[0]
                        self.message = remaining[1] if len(remaining) > 1 else ""
                else:
                    self.message = message
            else:
                self.message = message
        
        except Exception:
            self.message = message
    
    def _parse_rfc5424(self, message: str):
        """RFC 5424形式のメッセージをパース"""
        try:
            # VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA MSG
            parts = message.split(None, 7)
            
            if len(parts) >= 7:
                self.hostname = parts[2] if parts[2] != '-' else self.source_ip
                self.message = parts[7] if len(parts) > 7 else ""
            else:
                self.message = message
        
        except Exception:
            self.message = message


class SyslogReceiver(QObject):
    """Syslogメッセージ受信サーバー"""
    
    # シグナル定義
    message_received = pyqtSignal(object)  # SyslogMessage
    started = pyqtSignal()
    stopped = pyqtSignal()
    error_occurred = pyqtSignal(str)
    
    def __init__(self, parent=None):
        """
        初期化

        Args:
            parent: 親オブジェクト
        """
        super().__init__(parent)
        self.port = 514
        self.max_messages = 1000
        self.message_count = 0
        # プロトコルごとに独立して起動/停止する（UDP 稼働中に TCP を無停止で追加できる）
        # proto -> {"socket":…, "thread":…, "stop":Event, "port":int}
        self._servers = {}
        self.tcp_clients = []

    @property
    def is_running(self):
        """いずれかのプロトコルが稼働中か"""
        return bool(self._servers)

    @property
    def protocol(self):
        """稼働中プロトコル（後方互換。両方なら "UDP+TCP"）"""
        act = self.active_protocols()
        return "+".join(act) if act else "UDP"

    def active_protocols(self):
        """稼働中プロトコルを ["UDP","TCP"] の順で返す"""
        return [p for p in ("UDP", "TCP") if p in self._servers]

    def active_port(self, proto):
        """指定プロトコルの待受ポート（未稼働なら None）"""
        s = self._servers.get(str(proto).upper())
        return s["port"] if s else None

    def is_protocol_running(self, proto):
        """指定プロトコルが稼働中か"""
        return str(proto).upper() in self._servers

    def start(self, port: int = 514, protocol: str = "UDP"):
        """受信を開始。protocol は "UDP" / "TCP" / "UDP+TCP" を受け付ける。"""
        protos = [x for x in str(protocol).upper().replace("/", "+").split("+")
                  if x in ("UDP", "TCP")]
        if not protos:
            protos = ["UDP"]
        ok_any = False
        for proto in protos:
            if self.start_protocol(proto, port):
                ok_any = True
        return ok_any

    def start_protocol(self, proto: str, port: int = None):
        """1プロトコルだけ起動する（他方が稼働中でも無停止で追加できる）。冪等。"""
        proto = str(proto).upper()
        if proto not in ("UDP", "TCP"):
            self.error_occurred.emit("未対応のプロトコルです: %s" % proto)
            return False
        if proto in self._servers:
            return True
        # プロトコルごとに別ポートを指定できる（self.port は既定値としてのみ使う）
        use_port = self.port if port is None else port
        # 受信ポートの Windows ファイアウォール受信許可（Windowsのみ・冪等・必要時UAC）
        try:
            from .firewall import ensure_inbound_allow
            _ok, _msg = ensure_inbound_allow("Syslog", proto, use_port)
            print("[Syslog] ファイアウォール(%s): %s" % (proto, _msg))
        except Exception as _e:
            print("[Syslog] ファイアウォール設定エラー: %s" % _e)

        # bind はスレッド外で行い、失敗を呼び出し側へ即座に返す
        try:
            if proto == "UDP":
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                set_exclusive_bind(sock)
                sock.bind(("0.0.0.0", use_port))
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                set_exclusive_bind(sock)
                sock.bind(("0.0.0.0", use_port))
                sock.listen(5)
            sock.settimeout(1.0)
        except OSError as e:
            if getattr(e, "errno", None) == 10013:
                self.error_occurred.emit(
                    "ポート %d はWindowsで特権ポートです。\n1024以上のポート番号（例: 1514）を使用してください。" % use_port)
            else:
                self.error_occurred.emit("%s ポート %d のバインドに失敗: %s" % (proto, use_port, e))
            return False

        stop_event = threading.Event()
        entry = {"socket": sock, "thread": None, "stop": stop_event, "port": use_port}
        try:
            entry["port"] = sock.getsockname()[1]  # port=0 のとき実ポートを反映
        except Exception:
            pass
        target = self._run_udp_loop if proto == "UDP" else self._run_tcp_loop
        th = threading.Thread(target=target, args=(sock, stop_event, entry["port"]), daemon=True)
        entry["thread"] = th
        self._servers[proto] = entry
        th.start()
        print("[Syslog] %s Server started on port %d" % (proto, entry["port"]))
        self.started.emit()
        return True

    def stop_protocol(self, proto: str):
        """1プロトコルだけ停止する（他方は動き続ける）"""
        proto = str(proto).upper()
        entry = self._servers.pop(proto, None)
        if not entry:
            return
        entry["stop"].set()
        try:
            entry["socket"].close()
        except Exception:
            pass
        th = entry["thread"]
        if th and th.is_alive():
            th.join(timeout=2)
        print("[Syslog] %s Server stopped" % proto)
        if not self._servers:
            self.stopped.emit()

    def stop(self):
        """すべてのプロトコルを停止"""
        if not self._servers:
            return
        print("[Syslog] Stopping server...")
        for proto in list(self._servers.keys()):
            self.stop_protocol(proto)
        for thread in self.tcp_clients:
            if thread.is_alive():
                thread.join(timeout=1)
        self.tcp_clients.clear()
        print("[Syslog] Server stopped")

    def _run_udp_loop(self, sock, stop_event, listen_port=None):
        """UDP受信ループ"""
        try:
            while not stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(65535)
                    try:
                        message_str = data.decode("utf-8", errors="ignore")
                    except Exception:
                        message_str = data.decode("latin-1", errors="ignore")
                    self.message_received.emit(
                        SyslogMessage(message_str, addr[0], "UDP", listen_port))
                    self.message_count += 1
                except socket.timeout:
                    continue
                except Exception as e:
                    if stop_event.is_set():
                        break
                    print("[Syslog] Receive error: %s" % e)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _run_tcp_loop(self, sock, stop_event, listen_port=None):
        """TCP接続受付ループ"""
        try:
            while not stop_event.is_set():
                try:
                    client_socket, addr = sock.accept()
                    print("[Syslog] TCP client connected: %s" % addr[0])
                    client_thread = threading.Thread(
                        target=self._handle_tcp_client,
                        args=(client_socket, addr[0], stop_event, listen_port),
                        daemon=True,
                    )
                    client_thread.start()
                    self.tcp_clients.append(client_thread)
                except socket.timeout:
                    continue
                except Exception as e:
                    if stop_event.is_set():
                        break
                    print("[Syslog] Accept error: %s" % e)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _handle_tcp_client(self, client_socket, client_ip, stop_event, listen_port=None):
        """TCPクライアントからのメッセージを処理（改行区切り）"""
        try:
            client_socket.settimeout(1.0)
            buffer = b""
            while not stop_event.is_set():
                try:
                    data = client_socket.recv(4096)
                    if not data:
                        break
                    buffer += data
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            message_str = line.decode("utf-8", errors="ignore").strip()
                        except Exception:
                            message_str = line.decode("latin-1", errors="ignore").strip()
                        if message_str:
                            self.message_received.emit(
                                SyslogMessage(message_str, client_ip, "TCP", listen_port))
                            self.message_count += 1
                except socket.timeout:
                    continue
                except Exception as e:
                    print("[Syslog] TCP receive error: %s" % e)
                    break
        finally:
            try:
                client_socket.close()
            except Exception:
                pass
            print("[Syslog] TCP client disconnected: %s" % client_ip)
