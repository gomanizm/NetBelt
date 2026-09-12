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


class _ServerStopped(Exception):
    """停止要求により転送を打ち切った（タイムアウトとは区別する）"""


class _NetasciiDecoder:
    """netascii（RFC 1350）の受信バイト列を復号する。CR LF → LF、CR NUL → CR。

    CR がブロック境界の末尾に来ることがあるので、対を成す次の 1 バイトが
    届くまで CR を持ち越す。転送の終わりに残った CR は flush() で書き出す。
    """

    def __init__(self):
        self._pending_cr = False

    def feed(self, data):
        out = bytearray()
        for b in data:
            if self._pending_cr:
                self._pending_cr = False
                if b == 0x0A:
                    out.append(0x0A)
                    continue
                if b == 0x00:
                    out.append(0x0D)
                    continue
                out.append(0x0D)       # 対を成さない CR はそのまま
            if b == 0x0D:
                self._pending_cr = True
            else:
                out.append(b)
        return bytes(out)

    def flush(self):
        if self._pending_cr:
            self._pending_cr = False
            return b"\r"
        return b""


def _netascii_encode(data):
    """送信用の netascii 変換。LF → CR LF、CR → CR NUL（バイトごとで状態を持たない）。

    ローカルのテキストは LF 改行前提。CRLF 改行のファイルを netascii で
    送ると回線上は CR NUL CR LF になり、厳密なピアでは改行の手前に CR が
    1 つ増える（同サーバ経由の往復は無損失）。
    """
    return data.replace(b"\r", b"\r\x00").replace(b"\n", b"\r\n")


def _netascii_size(path):
    """netascii へ変換したあとのバイト数を数える。

    tsize（RFC 2349）と進捗の分母は「実際に回線へ乗るオクテット数」なので、
    変換で伸びるぶんを数え直さないと 100% を超える。変換はバイトごとに
    状態を持たないので、読み出し単位で区切って数えてよい。
    """
    size = 0
    with open(path, "rb") as f:
        while True:
            raw = f.read(65536)
            if not raw:
                return size
            size += len(_netascii_encode(raw))


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
        # 「まだ起動していない」と「停止された」は別物。_running だけで
        # 表すと、起動前の呼び出しを停止と誤認する。
        self._stopping = False
        # 進行中の転送スレッド。覚えないと stop() で止められない。
        self._workers = []
        self._workers_lock = threading.Lock()
        self._retries = 5      # タイムアウト時の再送回数
        self._timeout = 2.0    # 送受信タイムアウト秒
        # 同時に走らせる転送の上限。TFTP は無認証で 0.0.0.0 で待ち受け、
        # 各ワーカーが専用ソケットを bind したまま十数秒 (timeout x retries)
        # 生きるので、上限が無いと到達可能な任意のホストがスレッドと
        # エフェメラルポートを積み上げられる。
        # 超えたぶんは待たせずにその場で断る。TFTP のクライアントは待って
        # くれないので、キューに滞留させても再送とタイムアウトを増やすだけ。
        # 機器を数台まとめて扱う実運用なら 16 で足りる。
        self.max_workers = 16

    def start(self):
        os.makedirs(self.root_dir, exist_ok=True)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        set_exclusive_bind(self._sock)
        self._sock.bind(("0.0.0.0", self.port))
        self.port = self._sock.getsockname()[1]  # port=0 のとき実ポートを反映
        self._sock.settimeout(1.0)
        self._stopping = False
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        """待受と進行中の転送を止める。

        待受ソケットは、待受スレッドが抜けたことを確かめてから閉じる。
        recvfrom でブロック中のスレッドが使うハンドルを別スレッドから
        解放するのは Windows では不正で、アクセス違反になる。
        settimeout(1.0) があるので _running を落とせば1秒以内に抜ける。

        転送スレッドも待つ。待たないと、停止したはずの後にファイルが
        作られたり、終了時に書きかけのファイルが黙って切り詰められる。
        """
        self._stopping = True
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)   # 待受の1秒に対する余裕
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            if worker.is_alive():
                # 転送側は _timeout 秒で必ず戻ってくるので、その少し先まで待つ
                worker.join(timeout=self._timeout + 2)
        # 抜けきったと確認できたときだけ閉じる。まだ recvfrom の中に
        # いるなら、閉じるより開いたままにしておく方が安全。
        if self._sock and not (self._thread and self._thread.is_alive()):
            try:
                self._sock.close()
            except Exception:
                pass

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
                self._spawn(self._handle_wrq, pkt, addr)
            elif op == OP_RRQ:
                self._spawn(self._handle_rrq, pkt, addr)

    def _spawn(self, handler, pkt, addr):
        """転送スレッドを起こし、終わるまで覚えておく。

        上限に達していたら起こさずに断る。起こせなかったときも、
        呼び出し元の待受ループを巻き添えにしない。例外がそこまで届くと
        待受スレッドだけが終わり、is_running は True のままなので、UI は
        「起動中」を出し続けるのに何も受け付けない状態になる。
        """
        def run():
            try:
                handler(pkt, addr)
            finally:
                with self._workers_lock:
                    if thread in self._workers:
                        self._workers.remove(thread)

        thread = None
        with self._workers_lock:
            if len(self._workers) < self.max_workers:
                thread = threading.Thread(target=run, daemon=True)
                self._workers.append(thread)

        if thread is None:
            self._refuse(addr, "server busy")
            return

        try:
            thread.start()
        except Exception as e:
            with self._workers_lock:
                if thread in self._workers:
                    self._workers.remove(thread)
            print(f"[TFTP] 転送スレッドを起こせませんでした: {e}")
            self._refuse(addr, "server busy")

    def _refuse(self, addr, message):
        """要求を断ったことを相手へ伝える（黙って捨てない）。

        黙って捨てるとクライアントは再送を繰り返し、タイムアウトまで
        待たされる。断られたと分かればすぐ次の手を打てる。
        """
        try:
            _err(self._sock, addr, 0, message)
        except Exception:
            pass

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
            # RFC 2349: 1〜255 秒の整数だけ受諾する。範囲外・非数値は黙って無視
            #（OACK に含めなければクライアントは既定値で動く）
            try:
                t = int(opts["timeout"])
            except ValueError:
                t = 0
            if 1 <= t <= 255:
                out["timeout"] = str(t)
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
            self.on_event("protocol_error", addr[0],
                          (filename, "パス外への書き込みを拒否", "upload"))
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
        timeout = self._transfer_timeout(xs, neg)
        # transfer_started とファイル生成は「最初の DATA を受けてから」（確立後）に行う。
        # 重複WRQの敗者スレッドは DATA が来ないので、ファイルを作らず黙って撤退する
        #（複数スレッドが同じファイルを truncate し合う競合も防ぐ）。
        first_ack = _oack(neg) if neg else struct.pack("!HH", OP_ACK, 0)
        xs.sendto(first_ack, addr)
        deadline = time.monotonic() + timeout
        expected = 1
        received = 0
        last_ack = first_ack
        last_prog = 0.0
        established = False
        f = None
        # netascii は回線上の CR LF / CR NUL を復号して保存する（octet は素通し）
        decoder = _NetasciiDecoder() if mode == "netascii" else None
        try:
            retries = 0
            while True:
                if self._stopping:
                    # 停止された。ここまでに ACK した分は finally で確実に
                    # 書き出す（放置すると終了時に黙って消える）
                    if established:
                        self.on_event("interrupted", addr[0],
                                      (filename, "upload"))
                    return
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
                    if time.monotonic() < deadline:
                        continue  # 合意した timeout まではまだ待つ（停止確認のため小刻みに戻る）
                    retries += 1
                    if retries > self._retries:
                        if not established:
                            return  # DATA が一度も来ない = 孤児。started/error とも出さず撤退
                        raise
                    xs.sendto(last_ack, addr)  # 最後の ACK を再送
                    deadline = time.monotonic() + timeout
                    continue
                retries = 0
                deadline = time.monotonic() + timeout
                if struct.unpack("!H", data[:2])[0] != OP_DATA:
                    continue
                block = struct.unpack("!H", data[2:4])[0]
                chunk = data[4:]
                if block == expected:
                    if not established:
                        established = True
                        f = open(target, "wb")
                        self.on_event("transfer_started", addr[0], (filename, total, "upload"))
                    f.write(decoder.feed(chunk) if decoder else chunk)
                    received += len(chunk)
                    last_ack = struct.pack("!HH", OP_ACK, block)
                    if len(chunk) < blksize:
                        if decoder:
                            f.write(decoder.flush())
                        # 最終ブロック。ACK はファイルを閉じてから返す。バッファ付きの
                        # ファイルは close() で最後の書き出しをするので、ディスク満杯や
                        # 共有フォルダの切断はここで初めて分かる。先に ACK を返すと
                        # 機器は「送れた」と思って次へ進み、設定は欠けたまま残る
                        closing, f = f, None
                        try:
                            closing.close()
                        except OSError as e:
                            _err(xs, addr, 3, "Disk full or allocation exceeded")
                            self.on_event("protocol_error", addr[0],
                                          (filename, "アップロード失敗（保存できません）: %s" % e,
                                           "upload"))
                            return
                        xs.sendto(last_ack, addr)
                        break
                    xs.sendto(last_ack, addr)
                    now = time.monotonic()
                    if now - last_prog >= 0.2:
                        last_prog = now
                        self.on_event("transfer_progress", addr[0], (filename, received, total, "upload"))
                    expected = (expected + 1) & 0xFFFF
                else:
                    xs.sendto(struct.pack("!HH", OP_ACK, block), addr)  # 重複 DATA へ再 ACK
            self.on_event("transfer_complete", addr[0], (filename, received, total, "upload"))
            # 最終 ACK が落ちたときの再送に応えられるよう、閉じる前に少し待つ
            self._dally(xs, addr, last_ack, expected, blksize, timeout)
        except socket.timeout:
            self.on_event("protocol_error", addr[0],
                          (filename, "アップロードがタイムアウト", "upload"))
        except (OSError, struct.error) as e:
            try:
                _err(xs, addr, 0, str(e))   # 可能ならクライアントへ ERROR 応答（未定義エラーコード0）
            except Exception:
                pass                         # xs 自体が原因のエラーなら送信も失敗しうる。二次例外は無視
            self.on_event("protocol_error", addr[0],
                          (filename, "アップロード失敗: %s" % e, "upload"))
        finally:
            if f:
                f.close()
            xs.close()

    def _transfer_timeout(self, xs, neg):
        """転送で使う待ち時間（秒）を決め、ソケットの待ちを小刻みに設定する。

        OACK で timeout を受諾したらその値、無ければ既定の _timeout。
        ソケット自体の待ちは最長 1 秒にし、呼び出し側が締切まで recv を
        繰り返す。こうしないと長い timeout（最大 255 秒）を受諾したときに
        停止要求へ気づくのがその分遅れ、stop() が転送スレッドを待ちきれない。
        """
        timeout = float(neg.get("timeout", self._timeout))
        xs.settimeout(min(1.0, timeout))
        return timeout

    def _dally(self, xs, addr, last_ack, final_block, blksize, timeout):
        """最終 ACK 送信後、timeout 秒ほど待って再送された最終 DATA へ再 ACK する。

        RFC 1350 の「最終 ACK を送った側はしばらく待つ」。即座に閉じると、
        最終 ACK が落ちたときの再送に誰も応えず（Windows では ICMP Port
        Unreachable が返る）、ファイルは保存済みなのに機器側だけが失敗と
        判定する。停止要求にすぐ気づけるよう、短い待ちを繰り返す。
        """
        deadline = time.monotonic() + timeout
        while not self._stopping:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            xs.settimeout(min(0.2, remaining))
            try:
                data, a = xs.recvfrom(blksize + 4)
            except socket.timeout:
                continue
            except OSError:
                break
            if a != addr or len(data) < 4:
                continue
            op, block = struct.unpack("!HH", data[:4])
            if op == OP_DATA and block == final_block:
                xs.sendto(last_ack, addr)

    def _send_and_wait_ack(self, xs, packet, addr, expect_block, timeout=None):
        """packet を送り、block=expect_block の ACK を待つ。来なければ最大 _retries 回まで再送。
        古い/重複 ACK は無視。全再送失敗で socket.timeout を送出（呼び出し側が中断処理）。
        timeout は 1 回の待ち秒数（省略時は既定の _timeout）。"""
        if timeout is None:
            timeout = self._timeout
        for attempt in range(self._retries + 1):
            # ここで見ないと、再送を繰り返すあいだ（最大 12 秒）停止に
            # 気づかず、止めたあとも DATA を送り続けることになる。
            if self._stopping:
                raise _ServerStopped()
            xs.sendto(packet, addr)
            deadline = time.monotonic() + timeout
            deadline_tries = 0
            while True:
                try:
                    ack, src = xs.recvfrom(516)
                except socket.timeout:
                    if time.monotonic() < deadline and not self._stopping:
                        continue  # 合意した timeout まではまだ待つ
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
            self.on_event("protocol_error", addr[0],
                          (filename, "パス外への書き込みを拒否", "download"))
            return
        if not self.allow_download or not os.path.isfile(target):
            _err(xs, addr, 1, "File not found"); xs.close()
            self.on_event("protocol_error", addr[0],
                          (filename, "要求されたファイルがありません", "download")); return
        neg = self._neg_options(opts)
        blksize = int(neg.get("blksize", "512"))
        timeout = self._transfer_timeout(xs, neg)
        # netascii は変換で伸びるので、変換後のバイト列から blksize ずつ
        # 切り出す（ファイルの読み出し単位でブロックを作ると最終判定が狂う）。
        # tsize と進捗の分母も、同じ「回線へ乗るバイト数」で揃える
        encode = mode == "netascii"
        # transfer_started は確立後（最初の ACK 受領後）に初めて出す。重複RRQの敗者スレッドは
        # 最初の ACK が来ない（機器は勝者の TID にしか ACK しない）ので、何も出さず黙って撤退する。
        sent = 0
        last_prog = 0.0
        established = False
        try:
            total = _netascii_size(target) if encode else os.path.getsize(target)
            if "tsize" in neg:
                neg["tsize"] = str(total)  # 実際に転送するオクテット数を返す
            with open(target, "rb") as f:
                if neg:
                    try:
                        self._send_and_wait_ack(xs, _oack(neg), addr, 0, timeout)  # OACKにACK(0)。再送付き
                    except socket.timeout:
                        return  # 未確立: 重複RRQの孤児。started/error とも出さない
                    except _ServerStopped:
                        return  # 未確立のまま停止。何も出さない
                    established = True
                    self.on_event("transfer_started", addr[0], (filename, total, "download"))
                block = 1
                pending = b""
                while True:
                    if self._stopping:
                        if established:
                            self.on_event("interrupted", addr[0],
                                          (filename, "download"))
                        return
                    if encode:
                        while len(pending) < blksize:
                            raw = f.read(blksize)
                            if not raw:
                                break
                            pending += _netascii_encode(raw)
                        chunk, pending = pending[:blksize], pending[blksize:]
                    else:
                        chunk = f.read(blksize)
                    try:
                        self._send_and_wait_ack(xs, struct.pack("!HH", OP_DATA, block) + chunk, addr, block, timeout)
                    except socket.timeout:
                        if not established:
                            return  # block1 の ACK すら来ない = 孤児。黙って撤退
                        raise      # 確立後の中断は本物のエラー
                    except _ServerStopped:
                        if established:
                            self.on_event("interrupted", addr[0],
                                          (filename, "download"))
                        return
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
            self.on_event("protocol_error", addr[0],
                          (filename, "ダウンロードがタイムアウト", "download"))
        except (OSError, struct.error) as e:
            try: _err(xs, addr, 0, str(e))
            except Exception: pass
            self.on_event("protocol_error", addr[0],
                          (filename, "ダウンロード失敗: %s" % e, "download"))
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
    # サイズは object で渡す。int だと C++ の 32bit int へ丸められ、
    # 2GiB 超の転送が例外も出さず小さい値や負値として表示される
    transfer_started = pyqtSignal(str, str, object, str)       # ip, filename, total, direction
    transfer_progress = pyqtSignal(str, str, object, object, str)  # ip, filename, done, total, direction
    transfer_complete = pyqtSignal(str, str, object, object, str)  # ip, filename, done, total, direction
    # 利用者が止めたことによる中断。エラーではないので別の口にする
    transfer_interrupted = pyqtSignal(str, str, str)         # ip, filename, direction
    # 転送ごとのプロトコル事象。サーバ障害ではないのでモーダルにはしない
    protocol_event = pyqtSignal(str, str, str, str)          # ip, filename, reason, direction

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

    def _on_event(self, kind, ip, payload):
        # 複数の handler スレッドから呼ばれるため _tx は _tx_lock で保護。emit は Qt が
        # スレッド安全（クロススレッドはキュー化）なのでロック外で行う。
        if kind == "transfer_started":
            fn, total, d = payload
            # 方向まで含める。同じ機器が同名ファイルを送受で同時に扱うと、
            # 方向を落としたキーでは片方が他方を潰す。
            key = (ip, fn, d)
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
                active = (ip, fn, d) in self._tx
            if active:
                self.transfer_progress.emit(ip, fn, int(done), int(total), d)
        elif kind == "transfer_complete":
            fn, done, total, d = payload
            key = (ip, fn, d)
            with self._tx_lock:
                st = self._tx.get(key)
                if st is not None:
                    st["done"] = True
                    st["count"] -= 1
                    if st["count"] <= 0:
                        self._tx.pop(key, None)
            self.transfer_complete.emit(ip, fn, int(done), int(total), d)
        elif kind == "interrupted":
            # 利用者が止めたことによる中断。エラーではないので別の口へ流す。
            # 併せて台帳から降ろす（transfer_complete が来ないため、
            # 放置すると「進行中」の行が残る）。
            filename, direction = payload
            with self._tx_lock:
                self._tx.pop((ip, filename, direction), None)
            self.transfer_interrupted.emit(ip, filename, direction)
        elif kind == "protocol_error":
            # 重複RRQ/WRQ の敗者が出すタイムアウトは、その転送が成功済み or
            # まだ生存兄弟がいる間は握り潰す。全滅（兄弟ゼロ・未完了）なら
            # 本物の失敗として通す。
            filename, reason, direction = payload
            suppress = False
            if filename and "タイムアウト" in reason:
                # 重複要求の敗者が出すタイムアウトを握り潰す。方向を見ないと、
                # 同じ機器が同名ファイルを送受で同時に扱ったときに、
                # 片方の失敗で反対方向まで巻き添えにする。
                with self._tx_lock:
                    st = self._tx.get((ip, filename, direction))
                    if st is not None:
                        st["count"] -= 1
                        suppress = st["done"] or st["count"] > 0
                        if st["count"] <= 0:
                            self._tx.pop((ip, filename, direction), None)
            if not suppress:
                self.protocol_event.emit(ip, filename, reason, direction)
        else:
            self.client_activity.emit(ip, payload)
