"""送信の背圧: 接続が区切りを待たずに書けない間、端末に次を渡させない。

SSH と Telnet は、端末から渡された区切りを GUI スレッドでその場で書く
（send_command）。受信のためにチャネル／ソケットへ 0.1 秒の時間切れを
掛けているので、相手が読むのが遅く受信ウィンドウや送信バッファが
空かないと、書き込みが時間切れになる。Python の sendall は、失敗したとき
どこまで送れたかを返さないので、そのまま切断として扱うしかなかった。

そこで、待たずに書ける量しか渡さない。接続は has_pending_sends（いま
渡されても待たずに書けないなら True）を端末へ渡し、端末はそれが False に
なるまで次の区切りを渡さない。未送信の分は端末の列に残るので、マクロの
停止などで取り消せる。書けるようになったことを send_drained で知らせる
のが、ここの見張り役（DrainWatcher）。
"""
import select
import threading
import time

# 端末が 1 回に渡す区切り（InteractiveTerminal.SEND_CHUNK = 512 文字）を
# UTF-8 にしたときの最大のバイト数。受信ウィンドウにこれだけの空きがあれば、
# 区切り 1 つをウィンドウ待ちなしで書ける。ただし SSH はこの空きを待たない
# （補充を遅らせる機器では補充が来ず、送信が止まる）。1 バイトでも空けば
# 入る分を書き、残り（区切り 1 つ分まで）は接続が持ち越す
MIN_SEND_ROOM = 512 * 4


def socket_writable(sock) -> bool:
    """いまソケットへ待たずに書けるか。

    調べられないとき（閉じた後など）は True を返す。待たせずに送らせれば、
    送る側が失敗を送信エラーとして知らせる。
    """
    try:
        _, writable, _ = select.select([], [sock], [], 0)
    except (OSError, ValueError, TypeError):
        return True
    return bool(writable)


def wait_writable(sock, timeout: float) -> None:
    """ソケットへ書けるようになるまで、最大 timeout 秒待つ"""
    try:
        select.select([], [sock], [], timeout)
    except (OSError, ValueError, TypeError):
        time.sleep(0.01)


class DrainWatcher:
    """接続が書けない間だけ見張り、書けるようになったら 1 回知らせる。

    Args:
        busy: いま待たずに書けないなら True を返す関数。GUI スレッドと
            見張りのスレッドの両方から呼ぶ
        notify: 書けるようになったら見張りのスレッドから呼ぶ
            （接続の send_drained を出す）
        wait: 見張りの 1 回の待ち。既定は 10ms 眠る
    """

    POLL_SECONDS = 0.01

    def __init__(self, busy, notify, wait=None):
        self._busy = busy
        self._notify = notify
        self._wait = wait or (lambda: time.sleep(self.POLL_SECONDS))
        self._lock = threading.Lock()
        self._thread = None
        self._stopped = False

    def check(self) -> bool:
        """いま待たずに書けないか。書けないなら見張りを始めて True を返す

        止めた後は False を返す。切断済みの接続で端末を待たせ続けないため。
        """
        if self._stopped or not self._busy():
            return False
        with self._lock:
            if not self._stopped and self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="netbelt-send-drain", daemon=True)
                self._thread.start()
        return True

    def _run(self):
        drained = False
        try:
            while not self._stopped:
                if not self._busy():
                    drained = True
                    break
                self._wait()
        except Exception:
            drained = True   # 調べられないなら待たせない（送る側が失敗を知らせる）
        finally:
            # 知らせる前に印を外す。知らせを受けた端末が次を書いて、また
            # 書けなくなったときに、新しい見張りを始められるように
            with self._lock:
                self._thread = None
        if drained and not self._stopped:
            self._notify()

    def stop(self, timeout: float = 1.0) -> None:
        """見張りを止め、終わるまで最大 timeout 秒待つ（以後は知らせない）"""
        with self._lock:
            self._stopped = True
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
