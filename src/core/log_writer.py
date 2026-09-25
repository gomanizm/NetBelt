"""端末のログ記録のファイルへの書き込みを、記録ごとの専用スレッドで行う。

記録の write / flush / close を GUI スレッドで呼ぶと、記録先（共有フォルダ・
USB など）が応答しない間イベントループが止まり、全タブの描画・打鍵・停止
ボタンが効かなくなる（実測: 書き込みが 1 秒止まるたびに GUI が 2 秒止まった）。

GUI は書き終わりを短い間だけ待つ。普段のローカルディスクでは待ちの中で書き
終わるので、これまでと同じく「呼んだら書けている」。スレッドがファイルの
呼び出しに入ったまま STALL_WAIT を超えて戻らなければ「詰まっている」とみなし、
以後は待たずに積むだけにする（積んだ分がはけたら、また待つ側へ戻る）。
スレッドで起きた失敗は failure に残し、以後の write / flush / close でも例外として返す。
"""
import os
import queue
import threading
import time
import weakref


class _Core:
    """GUI と書き込みスレッドが共有する状態（スレッドは LogWriter 本体を持たない）"""

    def __init__(self, f):
        self.f = f
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.idle = threading.Event()
        self.idle.set()
        self.ops = 0            # 積んだがまだ終えていない操作の数
        self.backlog = 0        # 積んだがまだ書いていない文字数
        self.written = 0        # ファイル上で増えたバイト数（書けた分だけ）
        self.error = None       # スレッドで起きた最初の失敗
        self.in_call = False    # いまファイルの呼び出しの中にいるか

    def put(self, op, text=""):
        with self.lock:
            self.ops += 1
            self.backlog += len(text)
            self.idle.clear()
        self.queue.put((op, text))

    def run(self):
        while True:
            op, text = self.queue.get()
            try:
                # 失敗したあとは書かない（記録は失敗する前の所まで）。閉じるのは必ず行う
                if op == "close" or self.error is None:
                    self.in_call = True
                    if op == "write":
                        self.f.write(text)
                        # テキストモードなので LF は os.linesep に直ってから
                        # UTF-8 で書かれる。記録中ダイアログはこの値を見せる
                        # （GUI スレッドから os.path.getsize を呼ばないため）
                        self.written += len(
                            text.replace("\n", os.linesep).encode("utf-8"))
                    elif op == "flush":
                        self.f.flush()
                    else:
                        self.f.close()
            except Exception as e:
                if self.error is None:
                    self.error = e
            finally:
                self.in_call = False
                with self.lock:
                    self.ops -= 1
                    self.backlog -= len(text)
                    if self.ops == 0:
                        self.idle.set()
            if op == "close":
                return


class LogWriter:
    """記録ファイルの包み。write / flush / close / name / closed を持つ。"""

    # ファイルの呼び出しがこれより長く戻らなければ、詰まったとみなす（秒）
    STALL_WAIT = 0.1
    # スレッドが動き出せない（CPU が混んでいる）ときも、これより長くは待たない（秒）
    MAX_WAIT = 1.0

    def __init__(self, f, on_stall=None):
        self.name = f.name
        self._core = _Core(f)
        self._on_stall = on_stall   # 詰まったとみなしたときに GUI スレッドで呼ぶ
        self._stalled = False
        self._closing = False
        threading.Thread(target=self._core.run, name="log-writer",
                         daemon=True).start()
        # 閉じないまま捨てられたら閉じる（ファイルオブジェクトと同じ）
        weakref.finalize(self, self._core.put, "close").atexit = False

    @property
    def closed(self) -> bool:
        return self._core.f.closed

    @property
    def busy(self) -> bool:
        """積んだ操作がまだ残っているか"""
        return not self._core.idle.is_set()

    @property
    def backlog(self) -> int:
        """積んだがまだ書いていない文字数"""
        return self._core.backlog

    @property
    def written_bytes(self) -> int:
        """ファイル上で増えたバイト数（実際に書けた分だけ）"""
        return self._core.written

    @property
    def failure(self):
        """スレッドで起きた失敗（無ければ None）"""
        return self._core.error

    def wait(self, timeout: float) -> bool:
        """積んだ操作が全部終わるまで、最大 timeout 秒待つ"""
        return self._core.idle.wait(timeout)

    def write(self, text: str) -> None:
        self._check()
        self._submit("write", text, settle=False)

    def flush(self) -> None:
        self._check()
        self._submit("flush")
        self._check()

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            self._submit("close")
        if self._core.error is not None:
            raise self._core.error

    def _check(self) -> None:
        if self._core.error is not None:
            raise self._core.error
        if self._closing:
            raise ValueError("I/O operation on closed file.")

    def _submit(self, op, text="", settle=True) -> None:
        core = self._core
        if self._stalled and core.idle.is_set():
            self._stalled = False       # 積んだ分がはけた。また書き終わりを待つ
        core.put(op, text)
        if not settle or self._stalled:
            return
        started = time.monotonic()
        while not core.idle.wait(0.01):
            waited = time.monotonic() - started
            if waited >= self.MAX_WAIT or (waited >= self.STALL_WAIT
                                           and core.in_call):
                self._stalled = True
                if self._on_stall is not None:
                    self._on_stall()
                return
