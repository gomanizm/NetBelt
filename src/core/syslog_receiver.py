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
    
    @staticmethod
    def _skip_structured_data(rest: str):
        """STRUCTURED-DATA を読み飛ばし、その直後の位置を返す（見つからなければ None）。

        SD-ELEMENT は "[" から対応する "]" まで。パラメータ値は引用符で囲まれ、
        中の "]" や "\"" は "\\" でエスケープされるので、空白で区切ると壊れる。
        """
        if rest.startswith("-"):
            return 1
        if not rest.startswith("["):
            return None
        i = 0
        while i < len(rest) and rest[i] == "[":
            i += 1
            in_quote = False
            while i < len(rest):
                ch = rest[i]
                if ch == "\\" and in_quote:
                    i += 2
                    continue
                if ch == '"':
                    in_quote = not in_quote
                elif ch == "]" and not in_quote:
                    break
                i += 1
            else:
                return None  # 閉じ "]" が無い
            i += 1
        return i

    def _parse_rfc5424(self, message: str):
        """RFC 5424形式のメッセージをパース"""
        try:
            # VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA MSG
            parts = message.split(None, 6)

            if len(parts) >= 7:
                self.hostname = parts[2] if parts[2] != '-' else self.source_ip
                rest = parts[6]
                end = self._skip_structured_data(rest)
                if end is None:
                    # SD が壊れている: 欠落させず残り全体を本文にする
                    self.message = rest
                else:
                    self.message = rest[end:].lstrip(" ")
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
        # TCP で受け取る 1 行の上限。改行が来ないと受信バッファは
        # 際限なく伸び、走査も O(n^2) になって受信スレッドが停滞する。
        # 待受は 0.0.0.0 で、開始時にファイアウォールの受信許可も足すので、
        # LAN 上の認証されていないホストから引き起こせる。
        # RFC 5424 の 2048 オクテットは「最低これだけは受けよ」であって
        # 上限ではない。実機は長い行を出すので、実用と防御の釣り合いで 64KiB。
        self.max_line_bytes = 64 * 1024
        # TCP の同時接続数の上限。接続ごとにスレッドとソケットを持つので、
        # 何も送らないアイドル接続を張られるだけで際限なく積み上がる。
        # TFTP の max_workers と同じ考えで、超過分は accept 直後に閉じる。
        self.max_tcp_connections = 64

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
        # ファイアウォールは自動設定しない（3CDaemon 方式）。管理者昇格(UAC)を避けるため、
        # 受信許可は Windows 標準の初回プロンプト／既存の許可ルールに委ねる。
        # 自動で足すと、ポートを変えて使うたびポート名入りのルールが恒久登録され、
        # 停止しても消えずに残骸が増える。通らない環境は fix_firewall() で直す。
        print("[Syslog] ファイアウォール: 自動設定なし（Windowsの許可に委ねます）")

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

    def fix_firewall(self):
        """手動: Windows FW 受信許可を追加（管理者昇格/UAC）

        起動時には触らない（TFTP/FTP と同じ 3CDaemon 方式）。Windows の
        初回プロンプトを拒否したなどで受信が通らない環境の復旧用で、
        押したときだけ昇格する。稼働中のプロトコルぶんだけ足す。
        """
        try:
            from .firewall import ensure_inbound_allow, ensure_self_program_allow
            if not self._servers:
                return False, "受信していません"
            results = []
            for proto, server in list(self._servers.items()):
                ok, msg = ensure_inbound_allow("Syslog", proto, server.get("port"))
                print("[Syslog] ファイアウォール(%s): %s" % (proto, msg))
                results.append(ok)
            ok2, msg2 = ensure_self_program_allow()
            print("[Syslog] ファイアウォール(自exe): %s" % msg2)
            results.append(ok2)
            return all(results), msg2
        except Exception as e:
            print("[Syslog] ファイアウォール設定エラー: %s" % e)
            return False, str(e)

    def stop_protocol(self, proto: str):
        """1プロトコルだけ停止する（他方は動き続ける）"""
        proto = str(proto).upper()
        entry = self._servers.pop(proto, None)
        if not entry:
            return
        entry["stop"].set()
        # ソケットはループ自身に閉じさせる。recvfrom / accept でブロック中の
        # スレッドが使っているハンドルを別スレッドから解放すると、Windows では
        # そのブロック中の呼び出しがアクセス違反で落ちる。
        # UDP・TCP どちらのソケットにも settimeout(1.0) があるので、
        # stop_event を立てるだけで1秒以内にループを抜け、finally で
        # 自分のソケットを閉じる。
        th = entry["thread"]
        if th and th.is_alive():
            th.join(timeout=3)   # ソケットのタイムアウト1秒に対する余裕
            if th.is_alive():
                print("[Syslog] %s の受信スレッドが終了しません"
                      "（ポートが解放されない可能性があります）" % proto)
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

    @staticmethod
    def _decode_bytes(data):
        """受信バイト列を文字列にする。

        UTF-8 を strict で試し、失敗したら latin-1 に落とす。errors="ignore" だと
        latin-1 / Shift_JIS 等を吐く機器の非 UTF-8 バイトが本文からも raw からも
        黙って消え、フォールバックに到達しない。latin-1 は全バイトを 1 対 1 で
        文字にするので、少なくとも欠落はしない。
        """
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    def _run_udp_loop(self, sock, stop_event, listen_port=None):
        """UDP受信ループ"""
        try:
            while not stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(65535)
                    message_str = self._decode_bytes(data)
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
                    # 終わった接続のスレッドを外しておく。append するだけだと
                    # 接続を繰り返すほどリストが単調に増え、stop() まで
                    # 解放されない。入れ替えでなく in-place で詰めるのは、
                    # stop() が同じリストを走査しているため。
                    self.tcp_clients[:] = [
                        t for t in self.tcp_clients if t.is_alive()]
                    if len(self.tcp_clients) >= self.max_tcp_connections:
                        print("[Syslog] TCP client refused (limit %d): %s"
                              % (self.max_tcp_connections, addr[0]))
                        try:
                            client_socket.close()
                        except Exception:
                            pass
                        continue
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

    # RFC 6587 §3.4.1 octet-counting: "MSG-LEN SP SYSLOG-MSG"。SYSLOG-MSG は
    # PRI（"<"）で始まるので、改行区切りの行が数字で始まる場合と区別できる
    _OCTET_COUNT_RE = re.compile(rb"^(\d{1,9}) <")

    def _emit_tcp_line(self, line, client_ip, listen_port):
        """TCP で切り出した 1 メッセージを配信する（空行は捨てる）"""
        message_str = self._decode_bytes(line).strip()
        if message_str:
            self.message_received.emit(
                SyslogMessage(message_str, client_ip, "TCP", listen_port))
            self.message_count += 1

    def _handle_tcp_client(self, client_socket, client_ip, stop_event, listen_port=None):
        """TCPクライアントからのメッセージを処理

        RFC 6587 の octet-counting（"<len> <msg>"）と改行区切りの両方を受け付ける。
        どちらかはバッファ先頭で判定し、混在も許す。
        """
        try:
            client_socket.settimeout(1.0)
            buffer = b""
            while not stop_event.is_set():
                try:
                    data = client_socket.recv(4096)
                    if not data:
                        break
                    buffer += data
                    too_long = False
                    while buffer and not too_long:
                        m = self._OCTET_COUNT_RE.match(buffer)
                        if m:
                            # octet-counting: 宣言された長さぶんが揃うまで待つ
                            length = int(m.group(1))
                            if length > self.max_line_bytes:
                                too_long = True
                                break
                            end = m.end() - 1 + length
                            if len(buffer) < end:
                                break
                            self._emit_tcp_line(buffer[m.end() - 1:end], client_ip, listen_port)
                            buffer = buffer[end:]
                            continue
                        # 改行区切り。いま組み立てている 1 行が上限を超えたら、
                        # その相手との接続を切る。黙って切り捨てると障害解析に
                        # 要る末尾を失うので、切ったことは記録に残す。
                        #
                        # 「改行がまだ来ていないとき」に限ると、上限を超えた行が
                        # 終端の改行ごと 1 回の recv で届いた場合に素通りする。
                        # 見るのは受信バッファ全体ではなく、次の改行までの長さ。
                        newline_at = buffer.find(b"\n")
                        if newline_at == -1:
                            current_line = len(buffer)
                        else:
                            current_line = newline_at
                            # CRLF の CR は配信前に落とすので中身ではない。
                            # 数えると、同じ中身の行が LF なら通り CRLF なら
                            # 切られる
                            if buffer[newline_at - 1:newline_at] == b"\r":
                                current_line -= 1
                        if current_line > self.max_line_bytes:
                            too_long = True
                            break
                        if newline_at == -1:
                            break
                        line, buffer = buffer.split(b"\n", 1)
                        self._emit_tcp_line(line, client_ip, listen_port)
                    if too_long:
                        # 一覧に並ぶので、機器からの行と同じ RFC 3164 の形で
                        # 組み立てる。生の文言のまま渡すと、先頭の語が
                        # 日時やホスト名として食われて読めなくなる。
                        # PRI 12 = facility 1 (user) / severity 4 (Warning)
                        self.message_received.emit(SyslogMessage(
                            "<12>%s NetBelt 1行が %d バイトを超えたため、"
                            "この接続を切断しました"
                            % (datetime.now().strftime("%b %d %H:%M:%S"),
                               self.max_line_bytes),
                            client_ip, "TCP", listen_port))
                        self.message_count += 1
                        break
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
