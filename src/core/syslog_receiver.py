"""Syslogメッセージ受信サーバー"""
import socket
import threading
import re
import time
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
            
            # RFC 5424形式チェック（VERSION の次が本当に TIMESTAMP か）
            if self._has_rfc5424_header(message_after_pri):
                # RFC 5424形式
                self._parse_rfc5424(message_after_pri)
            else:
                # RFC 3164形式
                self._parse_rfc3164(message_after_pri)
        
        except Exception as e:
            print(f"[Syslog] Parse error: {e}")
            # パースエラーの場合はそのまま表示
            self.message = self.raw_message
    
    # RFC 3164 の TIMESTAMP は "Mmm dd hh:mm:ss" に限られる
    _RFC3164_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    _RFC3164_TIME_RE = re.compile(r'^\d{2}:\d{2}:\d{2}$')

    @classmethod
    def _has_rfc3164_timestamp(cls, parts):
        """先頭 3 語が RFC 3164 の日時（例: "Jan  1 00:00:00"）か"""
        if len(parts) < 3:
            return False
        month, day, tm = parts[0], parts[1], parts[2]
        if month not in cls._RFC3164_MONTHS:
            return False
        if not (day.isdigit() and 1 <= int(day) <= 31):
            return False
        return bool(cls._RFC3164_TIME_RE.match(tm))

    # RFC 5424 の TIMESTAMP は RFC 3339 の日時か NILVALUE "-" に限られる
    _RFC5424_TIMESTAMP_RE = re.compile(
        r'^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?'
        r'(?:[Zz]|[+-]\d{2}:\d{2})$')

    @classmethod
    def _has_rfc5424_timestamp(cls, parts):
        """2 語目が RFC 5424 の TIMESTAMP（RFC 3339 か "-"）か"""
        if len(parts) < 2:
            return False
        return parts[1] == '-' or bool(cls._RFC5424_TIMESTAMP_RE.match(parts[1]))

    @classmethod
    def _has_rfc5424_header(cls, message: str):
        """PRI 以降が RFC 5424 のヘッダで始まっているか

        VERSION があるだけ（`^\\d+\\s`）で RFC 5424 と決めると、日時を付けない
        機器の本文が「数字＋空白」で始まっただけで RFC 5424 側へ回される。
        あちらは日時の妥当性を見ないので 3 語目がホスト名として拾われ、本文が
        大きく欠ける（実測: "3 interfaces are down on rtr01 now" が
        hostname='are' message='now' になった）。VERSION は 1〜2 桁に限り、
        その次が本当に TIMESTAMP のときだけ RFC 5424 として扱う。
        """
        if not re.match(r'^\d{1,2}\s', message):
            return False
        return cls._has_rfc5424_timestamp(message.split(None, 2))

    def _parse_rfc3164(self, message: str):
        """RFC 3164形式のメッセージをパース"""
        try:
            # TIMESTAMP HOSTNAME MESSAGE の形式
            # 例: Jan  1 00:00:00 hostname message
            #
            # 日時が本当に日時のときだけ消費する。語数だけで決め打ちすると、
            # 日時を付けない機器（service timestamps log datetime を切った等）の
            # 本文の先頭 4 語が日時+ホスト名として黙って捨てられる。
            parts = message.split(None, 3)
            if self._has_rfc3164_timestamp(parts) and len(parts) == 4:
                # parts[0]: Month, parts[1]: Day, parts[2]: Time, parts[3]: Hostname + Message
                remaining = parts[3].split(None, 1)
                if len(remaining) >= 1:
                    self.hostname = remaining[0]
                    self.message = remaining[1] if len(remaining) > 1 else ""
                    return
            # 日時が無い・欠けている場合は、ホスト名は送信元 IP のまま、
            # 本文は PRI 以降の全文を残す
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
        # TCP の無通信タイムアウト（秒）。上限だけでは、1 バイトも送らない
        # 接続が枠を恒久的に占有し、上限ぶん張られると正規の機器の syslog が
        # 届かなくなる。最後に受信してからこの時間が過ぎた接続は切る。
        # 黙っている機器も切られるが、TCP syslog は次に送るときに繋ぎ直す。
        self.tcp_idle_timeout_seconds = 10 * 60
        # GUI へ渡したまま、まだ処理されていない件数の上限。
        # max_messages は GUI が受け取った後の保持件数で、受信スレッドから
        # GUI へ渡す Qt のキュー自体には上限が無い。GUI の処理能力
        # （実測 約540件/秒）を超えるバーストが来ると、そのぶんがすべて
        # キューに残り、収まるまで GUI が固まる。UDP には接続数の上限も
        # 1 行の上限も効かないので、認証の要らない LAN ホストから起こせる。
        # 超過中の受信は捨て、捨てた件数ははけた時点で 1 件だけ通知する。
        self.max_pending_messages = 1000
        self.dropped_message_count = 0
        self._pending_lock = threading.Lock()
        self._pending_messages = 0
        self._dropped_since_notice = 0
        # 自分の信号を自分でも受ける。受信スレッドから emit した分は
        # キュー経由で GUI スレッドへ届くので、このスロットが呼ばれた
        # ことが「GUI が 1 件処理した」の合図になる。
        self.message_received.connect(self._on_message_delivered)

    def _emit_message(self, message):
        """GUI へ 1 件渡す。配送待ちが上限に達している間は捨てる。

        Returns:
            bool: 渡したら True、捨てたら False
        """
        with self._pending_lock:
            if self._pending_messages >= self.max_pending_messages:
                self.dropped_message_count += 1
                self._dropped_since_notice += 1
                return False
            self._pending_messages += 1
        self.message_received.emit(message)
        return True

    def _on_message_delivered(self, _message):
        """GUI が 1 件処理したので配送待ちを戻す（GUI スレッドで動く）"""
        with self._pending_lock:
            if self._pending_messages > 0:
                self._pending_messages -= 1
            dropped = self._dropped_since_notice
            if self._pending_messages == 0 and dropped:
                self._dropped_since_notice = 0
            else:
                dropped = 0
        if dropped:
            # 一覧に並ぶので、機器からの行と同じ RFC 3164 の形で組み立てる。
            # PRI 12 = facility 1 (user) / severity 4 (Warning)
            self._emit_message(SyslogMessage(
                "<12>%s NetBelt 受信が追いつかず %d 件を取りこぼしました"
                % (datetime.now().strftime("%b %d %H:%M:%S"), dropped),
                "127.0.0.1"))

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
            from .firewall import (combine_results, ensure_inbound_allow,
                                   ensure_self_program_allow)
            if not self._servers:
                return False, "受信していません"
            results = []
            for proto, server in list(self._servers.items()):
                ok, msg = ensure_inbound_allow("Syslog", proto, server.get("port"))
                print("[Syslog] ファイアウォール(%s): %s" % (proto, msg))
                results.append((ok, msg))
            ok2, msg2 = ensure_self_program_allow()
            print("[Syslog] ファイアウォール(自exe): %s" % msg2)
            results.append((ok2, msg2))
            # すべて成功したときの文言は従来どおり自exe の結果
            return combine_results(results, success_message=msg2)
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
        if proto == "TCP":
            # 接続中のクライアントスレッドの終了も待つ。待たずに停止を
            # 通知すると、その後でスレッドが最後の受信を emit し、止めた
            # はずの一覧へ 1 件追加される。どのスレッドも待受と同じ
            # stop_event を見ていて、recv のタイムアウト 1 秒以内に抜ける
            for client in list(self.tcp_clients):
                if client.is_alive():
                    client.join(timeout=3)
            self.tcp_clients[:] = [t for t in self.tcp_clients if t.is_alive()]
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
                    self._emit_message(
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

    @staticmethod
    def _find_line_end(buffer):
        """改行区切りの終端（LF と NUL の早い方）の位置を返す。無ければ -1。

        NUL は RFC 6587 3.4.2 が触れている非透過フレーミングの終端で、
        Python 標準の SysLogHandler(socktype=SOCK_STREAM) は LF の代わりに
        これを付ける。NUL は次の LF の手前までだけ探す（行ごとに受信
        バッファの末尾まで走査しない）。
        """
        lf = buffer.find(b"\n")
        nul = buffer.find(b"\x00", 0, len(buffer) if lf == -1 else lf)
        return lf if nul == -1 else nul

    def _emit_tcp_line(self, line, client_ip, listen_port, octet_counted=False):
        """TCP で切り出した 1 メッセージを配信する（空行は捨てる）

        octet_counted=True（RFC 6587 §3.4.1）は宣言された長さぶんがそのまま
        本文なので、末尾の空白・タブ・NEL(U+0085)・NBSP(U+00A0) も原文のまま
        残す。両端を落とすと raw_message が受信原文と一致しなくなる。
        改行区切り（§3.4.2）の終端（LF / NUL）は切り出しの時点で落ちているので、落とすのは
        CRLF の CR 1 個だけにし、本文末尾の空白は残す。先頭の空白は、PRI の
        解釈を変えないよう従来どおり落とす。
        """
        decoded = self._decode_bytes(line)
        # 空行かどうかの判定だけは、どちらの方式でも strip 済みの値で行う
        if not decoded.strip():
            return
        if octet_counted:
            message_str = decoded
        else:
            if decoded.endswith("\r"):
                decoded = decoded[:-1]
            message_str = decoded.lstrip()
        self._emit_message(
            SyslogMessage(message_str, client_ip, "TCP", listen_port))
        self.message_count += 1

    def _flush_tcp_residual(self, buffer, client_ip, listen_port):
        """受信バッファに残った終端なしの 1 行を配信する（後始末の共通処理）。

        改行区切りは終端（LF / NUL）が来るまで行を配信しないので、終端を
        付けずに黙る送り手の最後の 1 件は、この接続を畳むときに配信しないと
        消える。長さは受信のたびに上限（max_line_bytes）と照合済みなので、
        上限を超えた行はここまで来ない。宣言した長さに足りない
        octet-counting のフレームは、欠けた本文なので配信しない。
        """
        if buffer and not self._OCTET_COUNT_RE.match(buffer):
            self._emit_tcp_line(buffer, client_ip, listen_port)

    def _handle_tcp_client(self, client_socket, client_ip, stop_event, listen_port=None):
        """TCPクライアントからのメッセージを処理

        RFC 6587 の octet-counting（"<len> <msg>"）と改行区切りの両方を受け付ける。
        どちらかはバッファ先頭で判定し、混在も許す。

        最後に受信してから tcp_idle_timeout_seconds を過ぎた接続は切断する。

        相手が閉じた・相手が RST で打ち切った・無通信で切った・停止要求で
        抜けた、のいずれで終わる場合も、残った終端なしの 1 行を配信してから
        畳む。1 行が上限を超えて切断した場合だけは配信しない（切り捨てた
        ことを通知した直後に、その切れ端を 1 件として出すことになるため）。
        """
        try:
            client_socket.settimeout(1.0)
            buffer = b""
            last_activity = time.monotonic()
            while not stop_event.is_set():
                try:
                    data = client_socket.recv(4096)
                    if not data:
                        # 相手が閉じた。LF の無い最後の 1 行も 1 件として配信する
                        # （捨てると、終端を付けずに閉じる送り手の最後の 1 件が
                        # 消える）
                        self._flush_tcp_residual(buffer, client_ip, listen_port)
                        return
                    last_activity = time.monotonic()
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
                            self._emit_tcp_line(buffer[m.end() - 1:end], client_ip,
                                                listen_port, octet_counted=True)
                            buffer = buffer[end:]
                            continue
                        # 改行区切り。いま組み立てている 1 行が上限を超えたら、
                        # その相手との接続を切る。黙って切り捨てると障害解析に
                        # 要る末尾を失うので、切ったことは記録に残す。
                        #
                        # 「改行がまだ来ていないとき」に限ると、上限を超えた行が
                        # 終端の改行ごと 1 回の recv で届いた場合に素通りする。
                        # 見るのは受信バッファ全体ではなく、次の改行までの長さ。
                        # 終端は LF と NUL の早い方（_find_line_end）
                        newline_at = self._find_line_end(buffer)
                        if newline_at == -1:
                            current_line = len(buffer)
                            # LF がまだ届いていないだけの CRLF も同じ扱いにする。
                            # 数えると、上限ちょうどの行が、CR と LF の間で
                            # 受信が切れたときだけ超過と判定されて切られる
                            if buffer.endswith(b"\r"):
                                current_line -= 1
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
                        line, buffer = buffer[:newline_at], buffer[newline_at + 1:]
                        self._emit_tcp_line(line, client_ip, listen_port)
                    if too_long:
                        # 一覧に並ぶので、機器からの行と同じ RFC 3164 の形で
                        # 組み立てる。生の文言のまま渡すと、先頭の語が
                        # 日時やホスト名として食われて読めなくなる。
                        # PRI 12 = facility 1 (user) / severity 4 (Warning)
                        self._emit_message(SyslogMessage(
                            "<12>%s NetBelt 1行が %d バイトを超えたため、"
                            "この接続を切断しました"
                            % (datetime.now().strftime("%b %d %H:%M:%S"),
                               self.max_line_bytes),
                            client_ip, "TCP", listen_port))
                        self.message_count += 1
                        # 切り捨てた残りは配信しない（切断を伝えた直後に
                        # 切れ端を 1 件として出すことになる）
                        return
                except socket.timeout:
                    # 無通信のまま上限を過ぎた接続は切って枠を返す
                    idle = self.tcp_idle_timeout_seconds
                    if idle and time.monotonic() - last_activity > idle:
                        print("[Syslog] TCP client idle for %ds, closing: %s"
                              % (idle, client_ip))
                        self._flush_tcp_residual(buffer, client_ip, listen_port)
                        return
                    continue
                except OSError as e:
                    # 相手が RST で打ち切った（SO_LINGER 0 での close。
                    # Windows では WSAECONNRESET）。FIN と違って recv は空を
                    # 返さず例外になるので、ここでも残りを配信しないと、
                    # 機器の reload や経路のセッション切断のたびに終端なしの
                    # 最後の 1 行が消える。
                    # 配信そのものが投げた例外もこの枝に来うるので、配信は
                    # 包んでおく（同じ例外をもう一度踏まないため）
                    print("[Syslog] TCP receive error: %s" % e)
                    try:
                        self._flush_tcp_residual(buffer, client_ip, listen_port)
                    except Exception as flush_error:
                        print("[Syslog] TCP flush error: %s" % flush_error)
                    return
                except Exception as e:
                    # 通信以外の異常。何が壊れたか分からないので、従来どおり
                    # 残りは配信せずに畳む
                    print("[Syslog] TCP receive error: %s" % e)
                    return
            # 停止要求で待受ループを抜けた。相手は閉じていないので、
            # ここでも残りを配信してから畳む
            self._flush_tcp_residual(buffer, client_ip, listen_port)
        finally:
            try:
                client_socket.close()
            except Exception:
                pass
            print("[Syslog] TCP client disconnected: %s" % client_ip)
