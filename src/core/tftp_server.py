"""TFTP サーバー（RFC1350 + RFC2347/2348 オプション）。QObject 非依存の TFTPServer と、
UI 通知用の TFTPServerManager(QObject) の2層構成。"""
import os
import socket
import struct
import threading
import time
from PyQt6.QtCore import QObject, pyqtSignal
from .sockets import set_exclusive_bind

OP_RRQ, OP_WRQ, OP_DATA, OP_ACK, OP_ERROR, OP_OACK = 1, 2, 3, 4, 5, 6


def _safe_join(root, filename):
    """root 配下の実パスを返す。root 外はエラー（パストラバーサル防止）。

    realpath を使うのは、abspath が '..' を畳むだけでシンボリックリンクや
    ジャンクションを解決しないため。root 配下にリンクを1つ置かれるだけで
    その先の任意の場所へ到達できてしまう（Windows のジャンクションは
    一般ユーザーでも作成できる）。
    """
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, filename.lstrip("/\\")))
    if os.path.commonpath([root_abs, target]) != root_abs:
        raise ValueError("path traversal blocked")
    return target


def _err(sock, addr, code, msg):
    sock.sendto(struct.pack("!HH", OP_ERROR, code) + msg.encode() + b"\x00", addr)


class TFTPServer:
    """低レベル TFTP サーバー（Qt 非依存）。on_event(kind, ip, payload) で通知。
    payload は transfer_started/progress/complete では (filename, total_or_done, ..., direction) のタプル、
    それ以外（error 等）では文字列。"""

    def __init__(self, port=69, root_dir="./tftp_root",
                 allow_upload=True, allow_download=True, on_event=None):
        self.port = port
        self.root_dir = root_dir
        self.allow_upload = allow_upload
        self.allow_download = allow_download
        self.on_event = on_event or (lambda *a: None)
        self._sock = None
        self._thread = None
        self._running = False
        self._retries = 5      # タイムアウト時の再送回数
        self._timeout = 2.0    # 送受信タイムアウト秒

    def start(self):
        os.makedirs(self.root_dir, exist_ok=True)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        set_exclusive_bind(self._sock)
        self._sock.bind(("0.0.0.0", self.port))
        self.port = self._sock.getsockname()[1]  # port=0 のとき実ポートを反映
        self._sock.settimeout(1.0)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _serve(self):
        while self._running:
            try:
                pkt, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception:
                break
            if len(pkt) < 2:
                continue
            op = struct.unpack("!H", pkt[:2])[0]
            # RRQ/WRQ ごとにスレッドを起こす。重複要求（機器の再送。送信元ポートが毎回
            # 変わる個体もある）の敗者スレッドは、確立できなければ各ハンドラ内で黙って撤退する。
            if op == OP_WRQ:
                threading.Thread(target=self._handle_wrq, args=(pkt, addr), daemon=True).start()
            elif op == OP_RRQ:
                threading.Thread(target=self._handle_rrq, args=(pkt, addr), daemon=True).start()

    @staticmethod
    def _parse_request(pkt):
        """WRQ/RRQ の filename, mode, options を返す。"""
        parts = pkt[2:].split(b"\x00")
        filename = parts[0].decode("utf-8", "replace")
        mode = parts[1].decode("ascii", "replace").lower() if len(parts) > 1 else "octet"
        opts = {}
        i = 2
        while i + 1 < len(parts) and parts[i]:
            opts[parts[i].decode("ascii", "replace").lower()] = parts[i + 1].decode("ascii", "replace")
            i += 2
        return filename, mode, opts

    def _neg_options(self, opts):
        """対応オプションのみ OACK 用に採用。blksize/tsize/timeout。"""
        out = {}
        if "blksize" in opts:
            try:
                bs = max(8, min(65464, int(opts["blksize"])))
                out["blksize"] = str(bs)
            except ValueError:
                pass
        if "timeout" in opts:
            out["timeout"] = opts["timeout"]
        if "tsize" in opts:
            out["tsize"] = opts["tsize"]  # RRQ では実サイズに上書きする
        return out

    def _handle_wrq(self, pkt, addr):
        filename, mode, opts = self._parse_request(pkt)
        xs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        xs.bind(("0.0.0.0", 0))
        xs.settimeout(self._timeout)
        try:
            target = _safe_join(self.root_dir, filename)
        except ValueError:
            _err(xs, addr, 2, "Access violation")
            xs.close()
            self.on_event("error", addr[0], "path traversal blocked: %s" % filename)
            return
        if not self.allow_upload:
            _err(xs, addr, 2, "Upload disabled")
            xs.close()
            return
        neg = self._neg_options(opts)
        try:
            total = int(opts.get("tsize", "0") or 0)  # tsize はクライアント宣言。pop 前の opts から読む
        except ValueError:
            total = 0  # 不正な tsize はハンドラスレッドを落とさず 0 扱い
        neg.pop("tsize", None)  # WRQ の tsize はクライアント宣言。ここでは echo せず簡略化
        blksize = int(neg.get("blksize", "512"))
        # transfer_started とファイル生成は「最初の DATA を受けてから」（確立後）に行う。
        # 重複WRQの敗者スレッドは DATA が来ないので、ファイルを作らず黙って撤退する
        #（複数スレッドが同じファイルを truncate し合う競合も防ぐ）。
        first_ack = _oack(neg) if neg else struct.pack("!HH", OP_ACK, 0)
        xs.sendto(first_ack, addr)
        expected = 1
        received = 0
        last_ack = first_ack
        last_prog = 0.0
        established = False
        f = None
        try:
            retries = 0
            while True:
                try:
                    data, a = xs.recvfrom(blksize + 4)
                    # RFC1350: 確立後は相手の TID(IP:port) が一致するものだけ受理する。
                    # 照合しないと、同一セグメントの任意ホストが期待ブロック番号の
                    # DATA を1発撃つだけで進行中の転送へ任意データを混入させられる。
                    if a != addr:
                        _err(xs, a, 5, "Unknown transfer ID")
                        continue
                    # 長さを見ずに unpack すると、空データグラム1発で struct.error に
                    # なり、進行中の転送が中断してしまう
                    if len(data) < 4:
                        continue
                except socket.timeout:
                    retries += 1
                    if retries > self._retries:
                        if not established:
                            return  # DATA が一度も来ない = 孤児。started/error とも出さず撤退
                        raise
                    xs.sendto(last_ack, addr)  # 最後の ACK を再送
                    continue
                retries = 0
                if struct.unpack("!H", data[:2])[0] != OP_DATA:
                    continue
                block = struct.unpack("!H", data[2:4])[0]
                chunk = data[4:]
                if block == expected:
                    if not established:
                        established = True
                        f = open(target, "wb")
                        self.on_event("transfer_started", addr[0], (filename, total, "upload"))
                    f.write(chunk)
                    received += len(chunk)
                    last_ack = struct.pack("!HH", OP_ACK, block)
                    xs.sendto(last_ack, addr)
                    now = time.monotonic()
                    if now - last_prog >= 0.2:
                        last_prog = now
                        self.on_event("transfer_progress", addr[0], (filename, received, total, "upload"))
                    expected = (expected + 1) & 0xFFFF
                    if len(chunk) < blksize:
                        break
                else:
                    xs.sendto(struct.pack("!HH", OP_ACK, block), addr)  # 重複 DATA へ再 ACK
            self.on_event("transfer_complete", addr[0], (filename, received, total, "upload"))
        except socket.timeout:
            self.on_event("error", addr[0], "WRQ timeout: %s" % filename)
        except (OSError, struct.error) as e:
            try:
                _err(xs, addr, 0, str(e))   # 可能ならクライアントへ ERROR 応答（未定義エラーコード0）
            except Exception:
                pass                         # xs 自体が原因のエラーなら送信も失敗しうる。二次例外は無視
            self.on_event("error", addr[0], "WRQ error: %s: %s" % (filename, e))
        finally:
            if f:
                f.close()
            xs.close()

    def _send_and_wait_ack(self, xs, packet, addr, expect_block):
        """packet を送り、block=expect_block の ACK を待つ。来なければ最大 _retries 回まで再送。
        古い/重複 ACK は無視。全再送失敗で socket.timeout を送出（呼び出し側が中断処理）。"""
        for attempt in range(self._retries + 1):
            xs.sendto(packet, addr)
            deadline_tries = 0
            while True:
                try:
                    ack, src = xs.recvfrom(516)
                except socket.timeout:
                    break  # この試行はタイムアウト。外側 for で再送
                # RFC1350: 相手の TID と一致しない ACK は受理せず、送信元へ通知する
                if src != addr:
                    _err(xs, src, 5, "Unknown transfer ID")
                    deadline_tries += 1
                    if deadline_tries > 8:
                        break
                    continue
                if len(ack) >= 4 and struct.unpack("!H", ack[:2])[0] == OP_ACK \
                        and struct.unpack("!H", ack[2:4])[0] == expect_block:
                    return  # 期待 ACK 受領
                deadline_tries += 1
                if deadline_tries > 8:
                    break  # 無関係パケットが続くとき暴走を防ぐ
        raise socket.timeout()

    def _handle_rrq(self, pkt, addr):
        filename, mode, opts = self._parse_request(pkt)
        xs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        xs.bind(("0.0.0.0", 0))
        xs.settimeout(self._timeout)
        try:
            target = _safe_join(self.root_dir, filename)
        except ValueError:
            _err(xs, addr, 2, "Access violation"); xs.close()
            self.on_event("error", addr[0], "path traversal blocked: %s" % filename)
            return
        if not self.allow_download or not os.path.isfile(target):
            _err(xs, addr, 1, "File not found"); xs.close()
            self.on_event("error", addr[0], "RRQ not found: %s" % filename); return
        neg = self._neg_options(opts)
        if "tsize" in neg:
            neg["tsize"] = str(os.path.getsize(target))  # 実サイズを返す
        blksize = int(neg.get("blksize", "512"))
        total = os.path.getsize(target)
        # transfer_started は確立後（最初の ACK 受領後）に初めて出す。重複RRQの敗者スレッドは
        # 最初の ACK が来ない（機器は勝者の TID にしか ACK しない）ので、何も出さず黙って撤退する。
        sent = 0
        last_prog = 0.0
        established = False
        try:
            with open(target, "rb") as f:
                if neg:
                    try:
                        self._send_and_wait_ack(xs, _oack(neg), addr, 0)  # OACKにACK(0)。再送付き
                    except socket.timeout:
                        return  # 未確立: 重複RRQの孤児。started/error とも出さない
                    established = True
                    self.on_event("transfer_started", addr[0], (filename, total, "download"))
                block = 1
                while True:
                    chunk = f.read(blksize)
                    try:
                        self._send_and_wait_ack(xs, struct.pack("!HH", OP_DATA, block) + chunk, addr, block)
                    except socket.timeout:
                        if not established:
                            return  # block1 の ACK すら来ない = 孤児。黙って撤退
                        raise      # 確立後の中断は本物のエラー
                    if not established:
                        established = True
                        self.on_event("transfer_started", addr[0], (filename, total, "download"))
                    sent += len(chunk)
                    now = time.monotonic()
                    if now - last_prog >= 0.2:
                        last_prog = now
                        self.on_event("transfer_progress", addr[0], (filename, sent, total, "download"))
                    block = (block + 1) & 0xFFFF
                    if len(chunk) < blksize:
                        break
            self.on_event("transfer_complete", addr[0], (filename, sent, total, "download"))
        except socket.timeout:
            self.on_event("error", addr[0], "RRQ timeout: %s" % filename)
        except (OSError, struct.error) as e:
            try: _err(xs, addr, 0, str(e))
            except Exception: pass
            self.on_event("error", addr[0], "RRQ error: %s: %s" % (filename, e))
        finally:
            xs.close()


def _oack(opts):
    body = b""
    for k, v in opts.items():
        body += k.encode() + b"\x00" + str(v).encode() + b"\x00"
    return struct.pack("!H", OP_OACK) + body


class TFTPServerManager(QObject):
    """TFTP サーバーの UI 向けラッパ。既存 SFTPServerManager と同じ役割。"""
    started = pyqtSignal()
    stopped = pyqtSignal()
    error_occurred = pyqtSignal(str)
    client_activity = pyqtSignal(str, str)
    transfer_started = pyqtSignal(str, str, int, str)       # ip, filename, total, direction
    transfer_progress = pyqtSignal(str, str, int, int, str)  # ip, filename, done, total, direction
    transfer_complete = pyqtSignal(str, str, int, int, str)  # ip, filename, done, total, direction

    def __init__(self, parent=None):
        super().__init__(parent)
        self._srv = None
        self.is_running = False
        # (ip, filename) 単位の表示コアレス状態。機器は RRQ/WRQ を送信元ポートを変えて
        # 複数回再送し、当サーバは各要求にスレッドを起こす（機器仕様で不可避）。敗者スレッドの
        # 重複 started と偽 timeout をここで束ね, UI には論理転送1本だけを見せる。
        self._tx = {}
        self._tx_lock = threading.Lock()

    def start(self, port=69, root_dir="./tftp_root", allow_upload=True, allow_download=True):
        if self.is_running:
            self.error_occurred.emit("サーバーは既に実行中です")
            return False
        # ファイアウォールは自動設定しない（3CDaemon 方式）。管理者昇格(UAC)を避けるため、
        # 受信許可は Windows 標準の初回プロンプト／既存の許可ルールに委ねる。過去にプロンプトを
        # 拒否してブロックが残っている場合のみ手動修正が要る（firewall.ensure_* は手動用に残置）。
        self.client_activity.emit("", "ファイアウォール: 自動設定なし（Windowsの許可に委ねます）")
        try:
            self._srv = TFTPServer(port=port, root_dir=root_dir,
                                   allow_upload=allow_upload, allow_download=allow_download,
                                   on_event=self._on_event)
            self._srv.start()
        except Exception as e:
            self.error_occurred.emit("TFTP起動失敗: %s" % e)
            self._srv = None
            return False
        self.is_running = True
        self.started.emit()
        return True

    def stop(self):
        if self._srv:
            self._srv.stop()
            self._srv = None
        self.is_running = False
        self.stopped.emit()

    def fix_firewall(self, port):
        """手動: Windows FW 受信許可を追加（管理者昇格/UAC）。3CDaemon 方式で通らない
        環境（過去のプロンプト拒否でブロック残り／FW通知が無効）の復旧用。押した時だけ昇格する。"""
        try:
            from .firewall import ensure_inbound_allow, ensure_self_program_allow
            ok, msg = ensure_inbound_allow("TFTP Server", "UDP", port)
            self.client_activity.emit("", "ファイアウォール: %s" % msg)
            ok2, msg2 = ensure_self_program_allow()
            self.client_activity.emit("", "ファイアウォール(自exe): %s" % msg2)
            return (ok and ok2), msg
        except Exception as e:
            self.error_occurred.emit("ファイアウォール設定エラー: %s" % e)
            return False, str(e)

    @staticmethod
    def _parse_timeout_filename(payload):
        """"RRQ timeout: <fn>" / "WRQ timeout: <fn>" から <fn> を取り出す。非該当は None。"""
        if isinstance(payload, str):
            for prefix in ("RRQ timeout: ", "WRQ timeout: "):
                if payload.startswith(prefix):
                    return payload[len(prefix):]
        return None

    def _on_event(self, kind, ip, payload):
        # 複数の handler スレッドから呼ばれるため _tx は _tx_lock で保護。emit は Qt が
        # スレッド安全（クロススレッドはキュー化）なのでロック外で行う。
        if kind == "transfer_started":
            fn, total, d = payload
            key = (ip, fn)
            with self._tx_lock:
                first = key not in self._tx
                if first:
                    self._tx[key] = {"count": 1, "done": False}
                else:
                    self._tx[key]["count"] += 1  # 再送で増えた重複は行を増やさない
            if first:
                self.transfer_started.emit(ip, fn, int(total), d)
        elif kind == "transfer_progress":
            fn, done, total, d = payload
            with self._tx_lock:
                active = (ip, fn) in self._tx
            if active:
                self.transfer_progress.emit(ip, fn, int(done), int(total), d)
        elif kind == "transfer_complete":
            fn, done, total, d = payload
            key = (ip, fn)
            with self._tx_lock:
                st = self._tx.get(key)
                if st is not None:
                    st["done"] = True
                    st["count"] -= 1
                    if st["count"] <= 0:
                        self._tx.pop(key, None)
            self.transfer_complete.emit(ip, fn, int(done), int(total), d)
        elif kind == "error":
            # 重複RRQ/WRQ の敗者が出す "…timeout: <fn>" は、その転送が成功済み or まだ
            # 生存兄弟がいる間は握り潰す。全滅（兄弟ゼロ・未完了）なら本物の失敗として通す。
            fn = self._parse_timeout_filename(payload)
            suppress = False
            if fn is not None:
                key = (ip, fn)
                with self._tx_lock:
                    st = self._tx.get(key)
                    if st is not None:
                        st["count"] -= 1
                        suppress = st["done"] or st["count"] > 0
                        if st["count"] <= 0:
                            self._tx.pop(key, None)
            if not suppress:
                self.error_occurred.emit(payload)
        else:
            self.client_activity.emit(ip, payload)
