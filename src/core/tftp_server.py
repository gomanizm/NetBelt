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

# 停止の期限を過ぎても転送スレッドが生き残っているときに、次の起動を断る
# 理由の文言。生き残りは保存先のファイルをまだ握っており（共有フォルダ相手の
# close() など）、そのまま起動すると新しいサーバが同名の WRQ を受理して、
# 後から復帰した旧 close() が中身を混ぜてしまう。FTP 側の
# ftp_server.PREVIOUS_STOP_INCOMPLETE_MESSAGE と同じ扱い
PREVIOUS_STOP_INCOMPLETE_MESSAGE = (
    "前回の停止が完了していません（進行中だった転送が終わっておらず、"
    "保存先のファイルを掴んだままの可能性があります）。"
    "しばらく待ってからもう一度お試しください")


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


# WRQ の tsize として使う値の上限（これ以上は大きさ不明として扱う）
_MAX_DECLARED_SIZE = 2 ** 63


def _declared_size(value):
    """WRQ の tsize（クライアントの申告）を解釈する。使えない値は 0（大きさ不明）。

    認証の無い相手が決める値なので、範囲を見ずに使うと 400 桁の数字が
    そのまま表示まで届き、float へ直すところで例外になる（負の値も通る）
    """
    try:
        size = int(value or 0)
    except ValueError:
        return 0
    return size if 0 <= size < _MAX_DECLARED_SIZE else 0


def _err(sock, addr, code, msg):
    sock.sendto(struct.pack("!HH", OP_ERROR, code) + msg.encode() + b"\x00", addr)


class _ServerStopped(Exception):
    """停止要求により転送を打ち切った（タイムアウトとは区別する）"""


class _PeerAborted(Exception):
    """相手が ERROR を送って転送を打ち切った（タイムアウトとは区別する）"""


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


def _netascii_size(path, should_stop=None):
    """netascii へ変換したあとのバイト数を数える。

    tsize（RFC 2349）と進捗の分母は「実際に回線へ乗るオクテット数」なので、
    変換で伸びるぶんを数え直さないと 100% を超える。変換はバイトごとに
    状態を持たないので、読み出し単位で区切って数えてよい。

    should_stop を渡すと、読み出しごとにそれを見て _ServerStopped を送出する。
    大きいファイルの走査は秒単位かかることがあり、打ち切れないと停止要求が
    走査の終わりまで待たされる（stop() は各ワーカーを順に join する）。
    """
    size = 0
    with open(path, "rb") as f:
        while True:
            if should_stop is not None and should_stop():
                raise _ServerStopped()
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
        # stop() の待ち時間を過ぎても終わらなかった転送スレッド。保存先の
        # ファイルをまだ握っているので、次の起動の可否を決めるのに使う
        self._unfinished = []
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
        # 最終 ACK の後の待ち（_dally）に入ったスレッド。転送は済んでいるので
        # 上の枠からは外してここへ移す（1 台から 0.2 秒おきに置くと約 30 本）。
        # これにも上限を設け、埋まっていれば待たずに閉じる
        self._dallying = []
        self.max_dallying = 64
        # 書き込み中の保存先（正規化した実パス）。同じ保存先へ 2 本の WRQ が
        # 同時に走ると、各ワーカーが独立に open(target, "wb") して 1 つの
        # ファイルへ交互に書き込み、どちらの機器にも「成功」を返しながら
        # 中身だけが混ざる。後から確立しようとした方はここで断る
        self._wrq_targets = set()
        self._targets_lock = threading.Lock()

    @staticmethod
    def _target_key(target):
        """保存先を比べるための鍵。

        _safe_join が realpath 済みの実パスを渡してくる（リンク・ジャンクションと、
        既存ファイルの 8.3 形式の短縮名はそこで解決される）。Windows は
        大文字小文字を区別しないので normcase で揃える。
        """
        return os.path.normcase(target)

    def _reserve_target(self, target):
        """保存先を 1 本の WRQ に予約する。取れたら True、先客がいれば False"""
        key = self._target_key(target)
        with self._targets_lock:
            if key in self._wrq_targets:
                return False
            self._wrq_targets.add(key)
            return True

    def _release_target(self, target):
        """保存先の予約を外す（取れていなくても呼んでよい）"""
        with self._targets_lock:
            self._wrq_targets.discard(self._target_key(target))

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

        待ちきれずに残った転送スレッドは覚えておき、全部終えられたかを
        返す（unfinished_workers を参照）。
        """
        self._stopping = True
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)   # 待受の1秒に対する余裕
        with self._workers_lock:
            workers = list(self._workers) + list(self._dallying)
        for worker in workers:
            if worker.is_alive():
                # 転送側は _timeout 秒で必ず戻ってくるので、その少し先まで待つ
                worker.join(timeout=self._timeout + 2)
        # 期限を過ぎても生きているスレッドは、保存先のファイルを握ったまま
        # （close() の中など）。捨てずに覚えておく
        for worker in workers:
            if worker.is_alive() and worker not in self._unfinished:
                self._unfinished.append(worker)
        # 抜けきったと確認できたときだけ閉じる。まだ recvfrom の中に
        # いるなら、閉じるより開いたままにしておく方が安全。
        if self._sock and not (self._thread and self._thread.is_alive()):
            try:
                self._sock.close()
            except Exception:
                pass
        return not self.unfinished_workers()

    def unfinished_workers(self):
        """停止しきれずに生き残っている転送スレッドを返す（待たない）。

        終わった分は落とすので、生き残りが消えれば空になる。
        """
        self._unfinished = [w for w in self._unfinished if w.is_alive()]
        return list(self._unfinished)

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
                    if thread in self._dallying:
                        self._dallying.remove(thread)

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
        # tsize はクライアント宣言。pop 前の opts から読む。不正・範囲外は 0 扱い
        total = _declared_size(opts.get("tsize"))
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
        reserved = False
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
                op = struct.unpack("!H", data[:2])[0]
                if op == OP_ERROR:
                    # 相手（TID は照合済み）が転送を打ち切った。読み捨てると
                    # タイムアウトまで ACK を再送し、ファイルも開いたままになる。
                    # 停止要求と同じ中断の経路で終える（ACK 済みの分は finally で
                    # 書き出す。確立前なら何も通知しない）
                    if established:
                        self.on_event("interrupted", addr[0],
                                      (filename, "upload"))
                    return
                if op != OP_DATA:
                    continue
                block = struct.unpack("!H", data[2:4])[0]
                chunk = data[4:]
                if block == expected:
                    if not established:
                        # 予約は確立時（最初の DATA を受けたとき）に取る。
                        # 要求を受けた時点で取ると、機器が WRQ を再送した
                        # ときの敗者スレッドまで断ってしまう（敗者には
                        # DATA が来ないので、ここまで来ない）
                        if not self._reserve_target(target):
                            _err(xs, addr, 0,
                                 "File busy: another upload is writing it")
                            # ファイル名は空で渡す。同じ (ip, filename, 方向)
                            # で走っている本物の転送の行を、この通知で
                            # 閉じてしまわないようにする
                            self.on_event("protocol_error", addr[0],
                                          ("", "他の転送が書き込み中のため断りました: %s"
                                           % filename, "upload"))
                            return
                        reserved = True
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
            # 書き込みは終わっている。完了を先に知らせてから予約を外す。逆順だと、
            # 外してから知らせるまでの間に同じ相手・同じ名前の次の WRQ が確立でき、
            # 後続がこの転送と同じ台帳の行へ相乗りする（先行の完了で done になり、
            # 後続の失敗が成功済みの重複として握り潰される）
            self.on_event("transfer_complete", addr[0], (filename, received, total, "upload"))
            # 最終 ACK の後の待ち（_dally）まで予約を握ると、同じファイルを
            # すぐ置き直す機器を断ってしまうので、待ちに入る前にここで外す
            if reserved:
                self._release_target(target)
                reserved = False
            # 最終 ACK が落ちたときの再送に応えられるよう、閉じる前に少し待つ。
            # 待つ間は同時転送の枠を空ける（待ちの枠が埋まっていれば待たない）
            if self._enter_dally():
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
            # バッファ付きファイルは close() で最後の書き出しをするので、中断で
            # 終わったときもここでディスク満杯などが初めて例外になる。そのまま
            # 上げると下の 2 文へ到達せず、保存先の予約が残って同名の置き直しを
            # File busy で断り続け（サーバを止めるまで直らない）、転送用ソケットも
            # 閉じられず、転送スレッドは未処理例外で落ちる
            close_error = None
            if f:
                try:
                    f.close()
                except OSError as e:
                    close_error = e
            try:
                if close_error is not None:
                    # 最終 DATA の経路と同じ知らせ方。「中断」だけを見て
                    # 保存できたと誤解させない。完了の通知と同じく、予約を
                    # 外すより先に出す。逆順だと、外してから知らせるまでの間に
                    # 同じ相手・同じ名前の次の WRQ が確立でき、この失敗通知が
                    # 後続の台帳の行を掴んで、成功する転送を「エラー」の行で
                    # 閉じてしまう（理由も先行のものが後続の名前で出る）
                    self.on_event("protocol_error", addr[0],
                                  (filename, "アップロード失敗（保存できません）: %s" % close_error,
                                   "upload"))
            finally:
                # 成功・失敗・停止・相手の ERROR・タイムアウトのどれで終わっても
                # 必ず外す。残すと、その保存先へ二度と書けなくなる。通知の側で
                # 例外が出てもここへ到達させるため finally に置く
                if reserved:
                    self._release_target(target)
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

    # 最終 ACK の後に待つ長さの下限と、長い timeout を受諾したときの上限（秒）
    DALLY_MIN_SECONDS = 6.0
    DALLY_MAX_SECONDS = 30.0

    def _enter_dally(self):
        """このスレッドを同時転送の枠から待ちの一覧へ移す。待ってよければ True。

        _dally は転送が済んだ後の待ちなので、同時転送の枠（max_workers）に
        数えたままだと、1 台から小さいファイルを続けて置くだけで枠が埋まり、
        次の要求を 'server busy' で断る。待ちの一覧にも上限（max_dallying）を
        設け、埋まっていれば False を返す（待たずに閉じる。失うのは最終 ACK が
        落ちたときの再 ACK だけ）。_spawn を通らずに呼ばれたときはそのまま待つ。
        """
        me = threading.current_thread()
        with self._workers_lock:
            if me not in self._workers:
                return True
            if len(self._dallying) >= self.max_dallying:
                return False
            self._workers.remove(me)
            self._dallying.append(me)
            return True

    def _dally(self, xs, addr, last_ack, final_block, blksize, timeout):
        """最終 ACK 送信後しばらく待ち、再送された最終 DATA へ再 ACK する。

        RFC 1350 の「最終 ACK を送った側はしばらく待つ」。即座に閉じると、
        最終 ACK が落ちたときの再送に誰も応えず（Windows では ICMP Port
        Unreachable が返る）、ファイルは保存済みなのに機器側だけが失敗と
        判定する。停止要求にすぐ気づけるよう、短い待ちを繰り返す。

        待つのは timeout の 3 倍（最低 DALLY_MIN_SECONDS）。timeout 1 回分
        だけだと、それより長い間隔で再送するクライアントや、再 ACK まで
        落ちた場合に間に合わない。再 ACK を返すたびに締切も延ばす（回数は
        _retries まで）。長い timeout（最大 255 秒）では DALLY_MAX_SECONDS で
        頭打ちにするが、従来の timeout 1 回分より短くはしない。
        この間はワーカー枠（max_workers）ではなく、_enter_dally で移した
        待ちの枠（max_dallying）を使う。
        """
        window = max(timeout, min(max(timeout * 3, self.DALLY_MIN_SECONDS),
                                  self.DALLY_MAX_SECONDS))
        deadline = time.monotonic() + window
        reacks = 0
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
                reacks += 1
                if reacks <= self._retries:
                    # この再 ACK も落ちたときの次の再送に備える
                    deadline = time.monotonic() + window

    def _send_and_wait_ack(self, xs, packet, addr, expect_block, timeout=None):
        """packet を送り、block=expect_block の ACK を待つ。来なければ最大 _retries 回まで再送。
        古い/重複 ACK は無視。全再送失敗で socket.timeout を送出（呼び出し側が中断処理）。
        相手の TID からの ERROR は _PeerAborted を送出する。
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
                if len(ack) >= 4 and struct.unpack("!H", ack[:2])[0] == OP_ERROR:
                    # 相手（TID は照合済み）が転送を打ち切った。読み捨てると
                    # タイムアウトまで DATA を再送し続け、その間ワーカーの枠と
                    # ファイルを掴んだままになる。WRQ 側と同じく、中断の経路で
                    # 終えられるよう呼び出し側へ伝える
                    raise _PeerAborted()
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
            # 走査中に停止されたら打ち切る（未確立なので何も通知しない）
            total = (_netascii_size(target, lambda: self._stopping) if encode
                     else os.path.getsize(target))
            if "tsize" in neg:
                neg["tsize"] = str(total)  # 実際に転送するオクテット数を返す
            with open(target, "rb") as f:
                if neg:
                    try:
                        self._send_and_wait_ack(xs, _oack(neg), addr, 0, timeout)  # OACKにACK(0)。再送付き
                    except socket.timeout:
                        return  # 未確立: 重複RRQの孤児。started/error とも出さない
                    except (_ServerStopped, _PeerAborted):
                        return  # 未確立のまま停止/打ち切り。何も出さない
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
                    except (_ServerStopped, _PeerAborted):
                        # 停止要求と、相手の ERROR による打ち切り。どちらも
                        # 失敗ではないので、確立済みなら中断として通知する
                        # （未確立＝重複 RRQ の敗者は黙って撤退）
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
        except _ServerStopped:
            return  # 事前走査の途中で停止。未確立なので何も通知しない
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
    # 未完了で終わった転送（利用者の停止・機器の打ち切り）。
    # エラーではないので別の口にする
    transfer_interrupted = pyqtSignal(str, str, str)         # ip, filename, direction
    # 転送ごとのプロトコル事象。サーバ障害ではないのでモーダルにはしない
    protocol_event = pyqtSignal(str, str, str, str)          # ip, filename, reason, direction

    def __init__(self, parent=None):
        super().__init__(parent)
        self._srv = None
        # 停止しきれなかった旧サーバ。生き残りの転送が消えるまで捨てない
        self._stopped_srv = None
        self.is_running = False
        # (ip, filename) 単位の表示コアレス状態。機器は RRQ/WRQ を送信元ポートを変えて
        # 複数回再送し、当サーバは各要求にスレッドを起こす（機器仕様で不可避）。敗者スレッドの
        # 重複 started と偽 timeout をここで束ね, UI には論理転送1本だけを見せる。
        self._tx = {}
        self._tx_lock = threading.Lock()
        # GUI へ渡したまま、まだ処理されていない protocol_event の件数の上限。
        # 失敗した要求（存在しないファイルへの RRQ など）ごとに 1 件出るので、
        # 認証の要らない相手が GUI の処理を上回る勢いで送ると、パネルのログの
        # 行数上限より手前の Qt の配送キューに際限なく積み上がる。Syslog の
        # max_pending_messages と同じく、超過中は数えるだけにして、はけた時点で
        # 省略した件数を 1 行だけ出す
        self.max_pending_notices = 1000
        self._notice_lock = threading.Lock()
        self._pending_notices = 0
        self._dropped_notices = 0
        # 自分の信号を自分でも受ける。ワーカーから emit した分はキュー経由で
        # GUI スレッドへ届くので、呼ばれたことが「GUI が 1 件処理した」の合図。
        # 転送の通知も同じカウンタに数える（存在する小さいファイルへの
        # RRQ→即 ACK を連打されると、成功した転送の通知だけで積み上がる）
        for signal in (self.protocol_event, self.transfer_started,
                       self.transfer_progress, self.transfer_complete,
                       self.transfer_interrupted):
            signal.connect(self._on_notice_delivered)

    def _take_notice(self, force=False, count_drop=True):
        """配送待ちを 1 件ぶん確保する。確保できたら True（呼び出し側が emit する）。

        上限に達している間は False。count_drop なら省略件数へ足す（進捗は
        次の進捗か完了で置き換わるので足さない）。force は上限を超えても
        確保する（開始を届けた転送の行を閉じる通知。捨てると行が残る）
        """
        with self._notice_lock:
            if not force and self._pending_notices >= self.max_pending_notices:
                if count_drop:
                    self._dropped_notices += 1
                return False
            self._pending_notices += 1
            return True

    def _take_closing_notice(self, st):
        """完了・中断を渡すかを決める（_tx_lock の中で呼ぶ）。

        開始を届けた行を閉じる最初の 1 件は必ず渡す。開始を省いた転送の
        ものは渡さずに省略件数へ足す。行の無いもの（台帳に無い・行を閉じた後）
        は上限の範囲でだけ渡す
        """
        row = st["row"] if st is not None else None
        if row == "shown":
            st["row"] = "closed"
            return self._take_notice(force=True)
        if row == "hidden":
            with self._notice_lock:
                self._dropped_notices += 1
            return False
        return self._take_notice()

    def _emit_protocol_event(self, ip, filename, reason, direction, force=False):
        """protocol_event を 1 件渡す。配送待ちが上限に達している間は数えるだけ。

        force は開始を届けた行をこの通知で閉じるとき（_take_notice を参照）
        """
        if self._take_notice(force=force):
            self.protocol_event.emit(ip, filename, reason, direction)

    def _on_notice_delivered(self, *_args):
        """GUI が通知を 1 件処理したので配送待ちを戻す（GUI スレッドで動く）"""
        with self._notice_lock:
            if self._pending_notices > 0:
                self._pending_notices -= 1
            dropped = 0
            if self._pending_notices == 0:
                dropped, self._dropped_notices = self._dropped_notices, 0
        if dropped:
            self.client_activity.emit(
                "", "表示が追いつかず %d 件の通知を省略しました" % dropped)

    def start(self, port=69, root_dir="./tftp_root", allow_upload=True, allow_download=True):
        if self.is_running:
            self.error_occurred.emit("サーバーは既に実行中です")
            return False
        # 前回の停止で終わりきらなかった転送が残っている間は起動しない。
        # 生き残りは保存先のファイルを握ったままなので、新しいサーバで
        # 同名の WRQ を受けると、後から復帰した旧 close() が中身を混ぜる。
        # ここでは待たない（画面を固めない）。消えていれば通す
        if not self._previous_stop_finished():
            self.error_occurred.emit(PREVIOUS_STOP_INCOMPLETE_MESSAGE)
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

    def _previous_stop_finished(self):
        """前回の停止が終わっているか。今の状態だけを見て、待たない。"""
        if self._stopped_srv is None:
            return True
        if self._stopped_srv.unfinished_workers():
            return False
        self._stopped_srv = None
        return True

    def stop(self):
        if self._srv:
            # 待ちきれなかった転送が残っているなら、その旧サーバは捨てない。
            # 捨てると生き残りの有無が分からなくなり、次の start() が
            # 同じ保存先への新しい WRQ を通してしまう
            finished = self._srv.stop()
            self._stopped_srv = None if finished else self._srv
            self._srv = None
        self.is_running = False
        self.stopped.emit()

    def fix_firewall(self, port):
        """手動: Windows FW 受信許可を追加（管理者昇格/UAC）。3CDaemon 方式で通らない
        環境（過去のプロンプト拒否でブロック残り／FW通知が無効）の復旧用。押した時だけ昇格する。"""
        try:
            from .firewall import (combine_results, ensure_inbound_allow,
                                   ensure_self_program_allow)
            ok, msg = ensure_inbound_allow("TFTP Server", "UDP", port)
            self.client_activity.emit("", "ファイアウォール: %s" % msg)
            ok2, msg2 = ensure_self_program_allow()
            self.client_activity.emit("", "ファイアウォール(自exe): %s" % msg2)
            return combine_results([(ok, msg), (ok2, msg2)])
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
            # row: 開始を届けた（shown）／配送待ちの上限で省いた（hidden）／
            # 行を閉じる完了・中断を届けた後（closed）
            with self._tx_lock:
                first = key not in self._tx
                shown = False
                if first:
                    shown = self._take_notice()
                    self._tx[key] = {"count": 1, "done": False,
                                     "row": "shown" if shown else "hidden"}
                else:
                    self._tx[key]["count"] += 1  # 再送で増えた重複は行を増やさない
            if shown:
                self.transfer_started.emit(ip, fn, int(total), d)
        elif kind == "transfer_progress":
            fn, done, total, d = payload
            with self._tx_lock:
                st = self._tx.get((ip, fn, d))
                show = (st is not None and st["row"] == "shown"
                        and self._take_notice(count_drop=False))
            if show:
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
                show = self._take_closing_notice(st)
            if show:
                self.transfer_complete.emit(ip, fn, int(done), int(total), d)
        elif kind == "interrupted":
            # 未完了で終わった転送（利用者の停止・機器の打ち切り）。
            # エラーではないので別の口へ流す。
            # 併せて台帳から降ろす（transfer_complete が来ないため、
            # 放置すると「進行中」の行が残る）。
            filename, direction = payload
            with self._tx_lock:
                st = self._tx.pop((ip, filename, direction), None)
                show = self._take_closing_notice(st)
            if show:
                self.transfer_interrupted.emit(ip, filename, direction)
        elif kind == "protocol_error":
            # 重複RRQ/WRQ の敗者が出すタイムアウトは、その転送が成功済み or
            # まだ生存兄弟がいる間は握り潰す。全滅（兄弟ゼロ・未完了）なら
            # 本物の失敗として通す。
            filename, reason, direction = payload
            suppress = False
            closing = False
            # 台帳から降ろす処理と、握り潰すかどうかの判定は分けて行う。
            # 理由文字列で降ろす／降ろさないを分けると、タイムアウト以外の
            # 失敗（保存できない・I/O エラー等）で項目が残り続け、以後その
            # (ip, filename, direction) の開始通知が出なくなるうえ、
            # done=True が焼き付いて本物のタイムアウトまで消える。
            # 方向を見ないと、同じ機器が同名ファイルを送受で同時に扱ったときに、
            # 片方の失敗で反対方向まで巻き添えにする。
            if filename:
                with self._tx_lock:
                    st = self._tx.get((ip, filename, direction))
                    if st is not None:
                        st["count"] -= 1
                        # 握り潰してよいのは、重複要求の敗者が出す偽の
                        # タイムアウトだけ。他の失敗は常に見せる。
                        if "タイムアウト" in reason:
                            suppress = st["done"] or st["count"] > 0
                        if st["count"] <= 0:
                            self._tx.pop((ip, filename, direction), None)
                        # パネルはこの通知で行を「エラー」に閉じる。完了と同じく、
                        # 開始を届けた行を閉じる通知は上限を超えても渡す
                        if not suppress and st["row"] == "shown":
                            st["row"] = "closed"
                            closing = True
            if not suppress:
                self._emit_protocol_event(ip, filename, reason, direction,
                                          force=closing)
        else:
            self.client_activity.emit(ip, payload)
