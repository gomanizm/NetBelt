"""内蔵 SFTP サーバーの開いているファイルの数（_OpenFiles）を、旧セッションの
記述子が実際に閉じるまで持っておくこと。

利用者の指示（2026-10-04）: 旧セッションのファイルが実際に閉じるまで枠を保持する。
満杯から連続で停止・再開し、旧・新セッション合算の SFTP のファイル数が 2048 を
超えないことを確かめる。古い終了の知らせが新しい枠を減らしたり、二重に返したり
しないことも確かめる。

何が起きていたか（実測、基準 43b2980。127.0.0.1）: session_ended が、残った
ハンドルを閉じる前にセッションの数をまとめて戻していた。開いたまま停止した
直後は、数 0 で記述子が数百個開いていた（回ごとに違う。満杯の 2048 個からでは
461 個）。その間に新しい起動のセッションが開けるので、旧セッションの残りと
合わせると 2048 を超えうる。

どう直したか: 1 個ぶん数えるたびに返す権利（_Slot）をハンドルに持たせ、記述子を
閉じたとき（close の後。例外が出ても）に 1 回だけ返す。session_ended は残った
ハンドルを 1 個ずつ閉じ、閉じた分だけ戻る。停止しても数を 0 に戻さない。

確かめ方:
- 数: _OpenFiles の数（錠の下で読む）。
- 記述子は 2 通りで数える。
  - 製品が os.open で開いた読み取りの記述子を、閉じるまで数える（_FdLedger）。
    _OpenFiles と同じ錠の下で数え、別のスレッドからも見張るので、「開いている
    記述子 <= 数 <= 上限」をどの時点でも矛盾なく確かめられる。
  - 動きが止まった時点で、a.txt を開いている CRT の記述子を os.fstat で数える
    （製品に手を入れずに測る）。
- 旧セッションの close を止める形（差し替えた close が許しを待つ。共有フォルダで
  close が詰まる形）と、止めない形の両方で見る。
"""
import contextlib
import io
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
PER_SESSION = 256
TOTAL = 2048
# UCRT の低水準ファイル入出力の記述子の上限（Windows のハンドル全体の上限ではない）
UCRT_MAX_FDS = 8192
# 応答と状態の変化を待つ上限（秒）。遅い CI と GC の停止（約 0.26 秒）を見込む
REPLY_TIMEOUT = 60.0
WAIT_SECONDS = 30.0
# 満杯から停止・再開する回数
RESTARTS = 5


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def open_many(sftp, count, path="/a.txt"):
    """a.txt の読み取りの OPEN を応答を待たずに count 回送る。(ハンドル, 断られた数)"""
    from paramiko.sftp import CMD_OPEN, SFTP_FLAG_READ
    from paramiko.sftp_attr import SFTPAttributes
    nums = [sftp._async_request(type(None), CMD_OPEN, path, SFTP_FLAG_READ,
                                SFTPAttributes()) for _ in range(count)]
    handles, refused = [], 0
    for num in nums:
        try:
            _t, msg = sftp._read_response(num)
        except IOError:
            refused += 1
            continue
        handles.append(msg.get_binary())
    return handles, refused


def run_together(count, target):
    """count 本のスレッドを Barrier で揃えて target(i) を動かし、結果を返す。

    スレッドの例外は結果に入れて呼び出し側で投げ直す（スレッドの中の
    assert は失敗にならないため）
    """
    barrier = threading.Barrier(count, timeout=REPLY_TIMEOUT)
    results = [None] * count

    def run(i):
        try:
            barrier.wait()
            results[i] = target(i)
        except BaseException as e:  # noqa: BLE001 - 呼び出し側で投げ直す
            results[i] = e

    threads = [threading.Thread(target=run, args=(i,), daemon=True)
               for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(REPLY_TIMEOUT * 2)
    alive = [t.name for t in threads if t.is_alive()]
    if alive:
        raise AssertionError("スレッドが終わらない: %s" % alive)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    return results


def file_identity(path):
    """path のファイルを見分ける (st_dev, st_ino)。記述子の os.fstat と同じ形で取る"""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        st = os.fstat(fd)
        return st.st_dev, st.st_ino
    finally:
        os.close(fd)


def fds_on(identity):
    """identity のファイルを開いている CRT の記述子の数（os.fstat が通る番号を調べる）"""
    count = 0
    for fd in range(UCRT_MAX_FDS + 64):
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == identity:
            count += 1
    return count


class _FdLedger:
    """製品（core.sftp_server）が os.open で読み取りに開いた記述子を、閉じるまで数える。

    数の変化は _OpenFiles の錠と自分の錠の両方の下で見るので、「開いている
    記述子 <= 数 <= 上限」を、開く・閉じるの途中でも矛盾なく確かめられる
    （製品は数を open の前に取り、記述子を閉じた後で返す）。数える側の増減は
    開いた後・閉じた後なので、数え漏れは「記述子が少なく見える」側にだけ出る。
    自分の錠は RLock（GC が閉じ忘れのファイルを閉じると、どのスレッドでも
    closed が呼ばれうるため）
    """

    def __init__(self, open_files, limit):
        self.of = open_files
        self.limit = limit
        self._lock = threading.RLock()
        self.live = 0
        self.peak_live = 0
        self.peak_count = 0
        self.problems = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def _check_locked(self, where):
        live, count = self.live, self.of._in_use
        self.peak_live = max(self.peak_live, live)
        self.peak_count = max(self.peak_count, count)
        if (live > count or count > self.limit) and len(self.problems) < 20:
            self.problems.append("%s: 記述子 %d・数 %d" % (where, live, count))

    def opened(self):
        with self.of._lock, self._lock:
            self.live += 1
            self._check_locked("open")

    def closed(self):
        with self._lock:
            self.live -= 1

    def _watch(self):
        while not self._stop.is_set():
            with self.of._lock, self._lock:
                self._check_locked("watch")
            time.sleep(0.001)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(10)


class _TrackedFileIO(io.FileIO):
    """閉じたら _FdLedger へ知らせる読み取りのファイル"""

    def __init__(self, fd, ledger):
        super().__init__(fd, "r", closefd=True)
        self._ledger = ledger
        self._counted = True

    def close(self):
        try:
            super().close()
        finally:
            counted, self._counted = getattr(self, "_counted", False), False
            if counted:
                self._ledger.closed()


def _under(root):
    """root の下のパスか（製品と同じく realpath で比べる）を見る関数を返す"""
    prefix = os.path.normcase(os.path.join(os.path.realpath(root), ""))
    return lambda path: os.path.normcase(os.fspath(path)).startswith(prefix)


class _OsWithLedger:
    """core.sftp_server の os の代わり。読み取りの open と fdopen だけ _FdLedger へつなぐ。

    数えるのは root（このテストのルート）の下のファイルだけ。差し替えはモジュール
    全体に効くので、前のテストのサーバーの後始末などは数えない
    """

    def __init__(self, ledger, root):
        self._ledger = ledger
        self._mine = _under(root)
        self._counted = set()   # 数えた記述子の番号（fdopen まで）

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args, **kwargs):
        fd = os.open(path, flags, *args, **kwargs)
        if not flags & (os.O_WRONLY | os.O_RDWR) and self._mine(path):
            self._counted.add(fd)
            self._ledger.opened()
        return fd

    def fdopen(self, fd, mode="r", *args, **kwargs):
        # 製品は読み取りだけの open を 'rb' で開く
        if fd not in self._counted:
            return os.fdopen(fd, mode, *args, **kwargs)
        self._counted.discard(fd)
        return io.BufferedReader(_TrackedFileIO(fd, self._ledger))


class _CloseGate:
    """paramiko.SFTPHandle.close を、開いているファイルなら許しが出るまで止める。

    共有フォルダで close が詰まる形を模す。止めるのは記述子を閉じる前。閉じ終えた
    ファイル（paramiko の後始末の 2 回目の close など）は止めずに通す。
    mode が "raise_before" なら閉じずに、"raise_after" なら閉じた後で例外を出す。
    差し替えはプロセス全体に効くので、止める・例外を出すのは root（このテストの
    ルート）の下のファイルだけにする（前のテストのサーバーが後始末で閉じる
    ファイルは、そのまま閉じる）
    """

    def __init__(self, root, mode="close"):
        import paramiko
        self._real_close = paramiko.SFTPHandle.close
        self._mine = _under(root)
        self.mode = mode
        self._permits = threading.Semaphore(0)
        self._opened = threading.Event()
        self._lock = threading.Lock()
        self.waiting = 0

    def close(self, handle):
        path = getattr(handle, "_real_path", None)
        if path is None or not self._mine(path):
            return self._real_close(handle)
        f = getattr(handle, "readfile", None) or getattr(handle, "writefile", None)
        if f is not None and not f.closed and not self._opened.is_set():
            with self._lock:
                self.waiting += 1
            try:
                # 許し（allow）か open() でだけ通す。時間切れで通すと、止めている
                # はずの close が数を減らす（Semaphore.acquire の期限は最初に
                # 待ち始めた時刻から数えるので、長く止めた close ほど切れやすい）
                while not self._permits.acquire(timeout=1.0):
                    if self._opened.is_set():
                        break
            finally:
                with self._lock:
                    self.waiting -= 1
        if self.mode == "raise_before":
            raise RuntimeError("close failed")
        self._real_close(handle)
        if self.mode == "raise_after":
            raise RuntimeError("close failed")

    def allow(self, count):
        """止まっている（これから止まる）close を count 個通す"""
        self._permits.release(count)

    def open(self):
        """これからの close を止めず、止まっている分もすべて通す"""
        self._opened.set()
        self._permits.release(100000)


class SlotTest(unittest.TestCase):
    """返す権利（_Slot）は、取った数え役の取ったセッションへ 1 回だけ返す"""

    def test_a_slot_gives_back_once_to_its_own_counter_and_session(self):
        from core.sftp_server import _OpenFiles
        old = _OpenFiles(3, 5)
        a, b = object(), object()
        slot_a, hit = old.take_slot(a)
        self.assertIsNone(hit)
        for _ in range(2):
            self.assertIsNotNone(old.take_slot(b)[0])
        self.assertEqual((old._in_use, old._held), (3, {a: 1, b: 2}))
        # 別の数え役（起動ごとに作り直した場合の、新しい起動の数の代わり）
        new = _OpenFiles(3, 5)
        c = object()
        self.assertIsNotNone(new.take_slot(c)[0])
        for _ in range(3):
            slot_a()
        self.assertEqual((old._in_use, old._held), (2, {b: 2}),
                         "同じ枠を 2 回以上返した")
        self.assertEqual((new._in_use, new._held), (1, {c: 1}),
                         "旧い枠の返却が、別の数え役の数を減らした")

    def test_take_slot_refuses_at_the_limits_without_counting(self):
        from core.sftp_server import _OpenFiles
        of = _OpenFiles(2, 3)
        a, b = object(), object()
        for _ in range(2):
            self.assertIsNotNone(of.take_slot(a)[0])
        self.assertEqual(of.take_slot(a), (None, "session"))
        self.assertIsNotNone(of.take_slot(b)[0])
        self.assertEqual(of.take_slot(b), (None, "total"))
        self.assertEqual((of._in_use, of._held), (3, {a: 2, b: 1}))

    def test_a_slot_called_from_many_threads_gives_back_once(self):
        from core.sftp_server import _OpenFiles
        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, interval)
        of = _OpenFiles(PER_SESSION, TOTAL)
        session = object()
        slots = [of.take_slot(session)[0] for _ in range(100)]
        # 半分の枠を 8 本のスレッドから同時に返す（同じ枠を 8 回ずつ）
        run_together(8, lambda i: [slot() for slot in slots[:50]])
        self.assertEqual((of._in_use, of._held), (50, {session: 50}))
        for slot in slots[50:]:
            slot()
        self.assertEqual((of._in_use, of._held), (0, {}))


class SessionEndTest(unittest.TestCase):
    """session_ended は、残ったハンドルを 1 個ずつ閉じ、閉じた分だけ戻る"""

    def setUp(self):
        from core.sftp_server import SFTPServerHandler, _OpenFiles
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-slot-")
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"abc")
        self.identity = file_identity(os.path.join(self.root, "a.txt"))
        self.of = _OpenFiles(3, 5)
        self.handler = SFTPServerHandler(None, self.root, open_files=self.of)

    def _open_three(self):
        from paramiko import SFTPHandle
        from paramiko.sftp_attr import SFTPAttributes
        handles = [self.handler.open("/a.txt", os.O_RDONLY, SFTPAttributes())
                   for _ in range(3)]
        self.assertTrue(all(isinstance(h, SFTPHandle) for h in handles))
        self.assertEqual((self.of._in_use, fds_on(self.identity)), (3, 3))
        return handles

    def test_every_left_file_is_closed_even_if_one_close_raises(self):
        import paramiko
        handles = self._open_three()
        real_close = paramiko.SFTPHandle.close
        mine = _under(self.root)
        calls = []

        def close(handle):
            path = getattr(handle, "_real_path", None)
            if path is None or not mine(path):
                # 差し替えはプロセス全体に効く。前のテストのサーバーが後始末で
                # 閉じるファイルは、数えずにそのまま閉じる（_CloseGate と同じ）
                return real_close(handle)
            # 例外を出すのは最初に呼ばれた 1 回（session_ended が回す順番は
            # 弱参照の集合の順番で決まらないので、ハンドルでは決めない）
            calls.append(handle)
            if len(calls) == 1:
                raise RuntimeError("close failed")
            real_close(handle)

        with mock.patch.object(paramiko.SFTPHandle, "close", close):
            self.handler.session_ended()   # 例外は外へ出さない
            self.assertEqual(len(calls), 3, "例外の後の残りを閉じに行かなかった")
            self.assertEqual(fds_on(self.identity), 0, "閉じずに残った記述子がある")
            self.assertEqual((self.of._in_use, self.of._held), (0, {}))
            # paramiko の finish_subsystem は、この後でも残ったハンドルを閉じる
            for h in handles:
                with contextlib.suppress(RuntimeError):
                    h.close()
        self.assertEqual((self.of._in_use, self.of._held), (0, {}),
                         "2 回目の close でもう一度戻した")

    def test_a_file_still_closing_keeps_its_slot(self):
        import paramiko
        # ハンドルは持っておく（製品では paramiko の表が閉じるまで持つ）
        handles = self._open_three()
        gate = _CloseGate(self.root)
        patcher = mock.patch.object(paramiko.SFTPHandle, "close",
                                    lambda handle: gate.close(handle))
        patcher.start()
        self.addCleanup(patcher.stop)
        ender = threading.Thread(target=self.handler.session_ended, daemon=True)
        # 後片付け（逆順に動く）: close を全部通してから、スレッドの終わりを待つ
        self.addCleanup(ender.join, REPLY_TIMEOUT)
        self.addCleanup(gate.open)
        ender.start()
        deadline = time.time() + WAIT_SECONDS
        while gate.waiting != 1 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(gate.waiting, 1, "前提: 最初の close で止まっていない")
        # close の中で止まっている間は、記述子も数も残る
        self.assertEqual((self.of._in_use, fds_on(self.identity)), (3, 3))
        gate.allow(1)
        deadline = time.time() + WAIT_SECONDS
        while self.of._in_use != 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual((self.of._in_use, fds_on(self.identity)), (2, 2))
        gate.open()
        ender.join(REPLY_TIMEOUT)
        self.assertFalse(ender.is_alive())
        self.assertEqual((self.of._in_use, self.of._held), (0, {}))
        self.assertEqual(fds_on(self.identity), 0)
        self.assertEqual(len(handles), 3)


class WriteCloseTest(unittest.TestCase):
    """書き込みだけのハンドルでも、close が閉じる前に例外を出したら、書き込み先の
    記述子を閉じてから 1 回だけ戻す（読み取りの記述子が無いハンドル）"""

    def setUp(self):
        from core.sftp_server import SFTPServerHandler, _OpenFiles, _OpenWriters
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-slot-")
        path = os.path.join(self.root, "w.txt")
        with open(path, "wb") as f:
            f.write(b"abc")
        self.identity = file_identity(path)
        self.of = _OpenFiles(3, 5)
        # サーバーと同じく書き込みの一覧を渡す（書き込みのハンドルになる）
        self.handler = SFTPServerHandler(None, self.root, open_files=self.of,
                                         open_writers=_OpenWriters())

    def test_a_write_close_that_raises_before_closing_gives_back_once_closed(self):
        import paramiko
        from paramiko.sftp_attr import SFTPAttributes
        handle = self.handler.open("/w.txt", os.O_WRONLY | os.O_TRUNC,
                                   SFTPAttributes())
        self.assertIsInstance(handle, paramiko.SFTPHandle)
        self.addCleanup(lambda: handle.writefile.close())
        self.assertIsNone(getattr(handle, "readfile", None),
                          "前提: 書き込みだけのハンドルではない")
        self.assertEqual((self.of._in_use, fds_on(self.identity)), (1, 1))
        real_close = paramiko.SFTPHandle.close
        mine = _under(self.root)

        def close(handle):
            path = getattr(handle, "_real_path", None)
            if path is None or not mine(path):
                # 前のテストのサーバーの後始末は、そのまま閉じる（_CloseGate と同じ）
                return real_close(handle)
            raise RuntimeError("close failed")   # 記述子を閉じる前に失敗する

        with mock.patch.object(paramiko.SFTPHandle, "close", close):
            with self.assertRaises(RuntimeError):
                handle.close()
            self.assertEqual((self.of._in_use, fds_on(self.identity)), (0, 0),
                             "書き込み先の記述子を閉じずに戻したか、戻していない")
            with self.assertRaises(RuntimeError):
                handle.close()
        self.assertEqual((self.of._in_use, self.of._held), (0, {}),
                         "2 回目の close でもう一度戻した")


class _ServerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        import paramiko
        cls.app = QApplication.instance() or QApplication([])
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-cap-")
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"abc")
        self.identity = file_identity(os.path.join(self.root, "a.txt"))

    def _limits(self, per_session, total):
        """上限を小さくする（マネージャを作る前に呼ぶ）"""
        from core.sftp_server import SFTPServerManager
        for name, value in (("MAX_OPEN_FILES_PER_SESSION", per_session),
                            ("MAX_OPEN_FILES_TOTAL", total)):
            p = mock.patch.object(SFTPServerManager, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _manager(self):
        from core.sftp_server import SFTPServerManager
        m = SFTPServerManager()
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.host_key = self.host_key
        # 後片付け（逆順に動く）: 止める → 旧セッションのファイルが閉じ終わるのを
        # 待つ → 配送待ちの通知を処理する
        self.addCleanup(self._flush_qt)
        self.addCleanup(self._wait, lambda: m._open_files._in_use == 0)
        self.addCleanup(m.stop)
        return m

    def _start(self, m):
        for _ in range(5):
            # 調べたポートを別のプロセスが先に取ることがある（排他の待受なので
            # start が失敗する）。そのときは別のポートで起動し直す
            self.port = free_port()
            if m.start(port=self.port, root_dir=self.root,
                       username=USER, password=PASSWORD):
                break
        else:
            self.fail("サーバーが起動しない")
        self.assertTrue(self._wait(lambda: m.is_running), "サーバーが起動しない")
        return m

    def _flush_qt(self):
        """配送待ちの通知（接続・切断・断った知らせ）を処理しておく"""
        for _ in range(3):
            self.app.processEvents()

    def _connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                  allow_agent=False, look_for_keys=False, timeout=10)
        self.addCleanup(c.close)
        return c

    def _session(self, client):
        s = client.open_sftp()
        s.get_channel().settimeout(REPLY_TIMEOUT)
        self.addCleanup(s.close)
        return s

    @staticmethod
    def _wait(predicate, seconds=WAIT_SECONDS):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    @staticmethod
    def _opens(sftp):
        """a.txt をもう 1 個開けたらハンドル、断られたら None"""
        try:
            return sftp.open("a.txt", "rb")
        except IOError:
            return None

    def _ledger(self, m):
        """製品の os を差し替え、開いている記述子を数え始める"""
        import core.sftp_server as sftp_server
        ledger = _FdLedger(m._open_files, m.MAX_OPEN_FILES_TOTAL)
        p = mock.patch.object(sftp_server, "os", _OsWithLedger(ledger, self.root))
        p.start()
        self.addCleanup(p.stop)
        ledger.start()
        self.addCleanup(ledger.stop)
        return ledger

    def _gate(self, mode="close"):
        """paramiko.SFTPHandle.close を _CloseGate で止める（後片付けで全部通す）"""
        import paramiko
        gate = _CloseGate(self.root, mode)
        p = mock.patch.object(paramiko.SFTPHandle, "close",
                              lambda handle: gate.close(handle))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(gate.open)
        return gate

    def _assert_ledger_ok(self, ledger):
        self.assertEqual(ledger.problems, [],
                         "記述子が数より多いか、数が上限を超えた瞬間があった")
        self.assertLessEqual(ledger.peak_live, ledger.limit)
        self.assertLessEqual(ledger.peak_count, ledger.limit)


class RestartFromFullTest(_ServerTestBase):
    """本物の上限（1 セッション 256・全体 2048）で、満杯から停止・即座の再開を繰り返す"""

    def setUp(self):
        super().setUp()
        self.m = self._start(self._manager())
        self.of = self.m._open_files
        self.assertEqual((self.m.MAX_OPEN_FILES_PER_SESSION,
                          self.m.MAX_OPEN_FILES_TOTAL), (PER_SESSION, TOTAL))
        self.ledger = self._ledger(self.m)

    def _fill(self):
        """2 接続 x 4 セッション x 256 個で、全体の上限まで開く"""
        for client in (self._connect(), self._connect()):
            for _ in range(TOTAL // PER_SESSION // 2):
                handles, refused = open_many(self._session(client), PER_SESSION)
                self.assertEqual((len(handles), refused), (PER_SESSION, 0))
        self.assertEqual((self.of._in_use, fds_on(self.identity)), (TOTAL, TOTAL))

    def _restart(self, round_no):
        self.m.stop()
        self._start(self.m)
        self.assertIs(self.m._open_files, self.of,
                      "起動し直したら数え役が変わった（%d 回目）" % round_no)

    def _record_opens(self):
        """このマネージャで開いたハンドルを、開いたセッション（SFTPServerHandler）と覚える"""
        import paramiko
        import core.sftp_server as sftp_server
        real_open = sftp_server.SFTPServerHandler._open
        opened = []

        def recording(handler, *args, **kwargs):
            result = real_open(handler, *args, **kwargs)
            if isinstance(result, paramiko.SFTPHandle) and \
                    handler._open_files is self.of:
                opened.append((handler, result))
            return result

        p = mock.patch.object(sftp_server.SFTPServerHandler, "_open", recording)
        p.start()
        self.addCleanup(p.stop)
        return opened

    def test_restarts_with_stuck_old_closes_stay_within_the_limit(self):
        """旧セッションの close が詰まっている間は、その分を新しい起動で使わない。

        停止・即座の再開を 5 回。毎回、旧セッションの close を FREED 個だけ通し、
        通した分だけ新しいセッションで開ける。新しいセッションが開いた後に届いた
        旧セッションの close（LATE 個）も、新しいセッションの数を減らさない。
        最後に、古い知らせ（旧セッションの session_ended と close）をもう一度
        届けても、数は変わらない
        """
        freed, late = 100, 20
        opened = self._record_opens()
        self._fill()
        gate = self._gate()
        current = None
        for round_no in range(1, RESTARTS + 1):
            self._restart(round_no)
            # 旧セッションの記述子はどれもまだ開いている。数も残っている
            self.assertEqual(self.of._in_use, TOTAL,
                             "停止で数が戻った（%d 回目）" % round_no)
            self.assertEqual(fds_on(self.identity), TOTAL)
            s = self._session(self._connect())
            before = set(self.of._held)
            self.assertEqual(open_many(s, 10), ([], 10),
                             "旧セッションの記述子が開いたまま、新しい起動で開けた")
            # 旧セッションの close を通した分だけ、記述子と数が一緒に減る
            gate.allow(freed)
            self.assertTrue(self._wait(lambda: self.of._in_use == TOTAL - freed),
                            "数が %d（%d 回目）" % (self.of._in_use, round_no))
            self.assertEqual(fds_on(self.identity), TOTAL - freed)
            handles, refused = open_many(s, freed + 50)
            self.assertEqual((len(handles), refused), (freed, 50))
            (current,) = set(self.of._held) - before
            # 新しいセッションが開いた後に届いた旧セッションの close
            gate.allow(late)
            self.assertTrue(self._wait(lambda: self.of._in_use == TOTAL - late),
                            "数が %d（%d 回目）" % (self.of._in_use, round_no))
            self.assertEqual(self.of._held[current], freed,
                             "旧セッションの close が新しいセッションの数を減らした")
            self.assertEqual(fds_on(self.identity), TOTAL - late)
            handles, refused = open_many(s, late + 5)
            self.assertEqual((len(handles), refused), (late, 5))
            self.assertEqual((self.of._in_use, fds_on(self.identity)), (TOTAL, TOTAL))

        # 旧セッションの close をすべて通すと、残るのは最後のセッションの分だけ
        gate.open()
        mine = freed + late
        self.assertTrue(self._wait(lambda: self.of._in_use == mine),
                        "数が %d" % self.of._in_use)
        time.sleep(0.3)   # paramiko の後始末の 2 回目の close で戻しすぎるなら、ここまでに
        self.assertEqual((self.of._in_use, self.of._held), (mine, {current: mine}))
        self.assertEqual(fds_on(self.identity), mine)
        # 古い知らせをもう一度届けても、数は変わらない
        old = [(handler, h) for handler, h in opened if handler is not current]
        self.assertEqual(len(old), TOTAL + (RESTARTS - 1) * mine)
        for handler in {handler for handler, _h in old}:
            handler.session_ended()
        for _handler, h in old:
            h.close()
        self.assertEqual((self.of._in_use, self.of._held), (mine, {current: mine}),
                         "古い知らせで数が変わった")
        self.m.stop()
        self.assertTrue(self._wait(lambda: self.of._in_use == 0),
                        "停止の後も数が残った: %d" % self.of._in_use)
        self.assertEqual(self.of._held, {})
        self.assertEqual(fds_on(self.identity), 0)
        self._assert_ledger_ok(self.ledger)

    def test_restarts_without_delay_stay_within_the_limit(self):
        """close を止めない普段の形。再開のたびに、すぐ上限より多く開く。

        旧セッションの後始末と新しいセッションの open が重なっても、記述子と数は
        2048 を超えない（_FdLedger が見張る）。後始末が済んだら、満杯まで開き直す
        """
        self._fill()
        for round_no in range(1, RESTARTS + 1):
            self._restart(round_no)
            clients = [self._connect() for _ in range(2)]
            sessions = [self._session(c) for c in clients for _ in range(4)]
            results = run_together(
                len(sessions), lambda i: open_many(sessions[i], PER_SESSION + 44))
            granted = sum(len(handles) for handles, _ in results)
            self.assertLessEqual(granted, TOTAL)
            self.assertLessEqual(max(len(handles) for handles, _ in results),
                                 PER_SESSION)
            # 旧セッションが閉じ終えると、数は新しいセッションの分だけになる
            self.assertTrue(self._wait(lambda: self.of._in_use == granted),
                            "数が %d・開けたのは %d（%d 回目）"
                            % (self.of._in_use, granted, round_no))
            self.assertEqual(fds_on(self.identity), granted)
            # 満杯まで開き直す（次の停止の前提）
            topped = sum(len(open_many(s, PER_SESSION)[0]) for s in sessions)
            self.assertEqual(granted + topped, TOTAL)
            self.assertEqual((self.of._in_use, fds_on(self.identity)), (TOTAL, TOTAL))
        self.m.stop()
        self.assertTrue(self._wait(lambda: self.of._in_use == 0),
                        "停止の後も数が残った: %d" % self.of._in_use)
        self.assertEqual(fds_on(self.identity), 0)
        self._assert_ledger_ok(self.ledger)


class SlotFollowsDescriptorTest(_ServerTestBase):
    """close が例外を出す・セッションの途中の切断・停止で、数は記述子が閉じたときだけ減る"""

    def setUp(self):
        super().setUp()
        self._limits(per_session=3, total=5)
        self.m = self._start(self._manager())
        self.of = self.m._open_files
        self.ledger = self._ledger(self.m)

    def _counts(self):
        return self.of._in_use, fds_on(self.identity)

    def _close_raises(self, mode):
        from paramiko.sftp import CMD_CLOSE
        s = self._session(self._connect())
        handles = [s.open("a.txt", "rb") for _ in range(3)]
        gate = self._gate(mode)
        raw = handles[0].handle
        num = s._async_request(type(None), CMD_CLOSE, raw)
        self.assertTrue(self._wait(lambda: gate.waiting == 1), "前提: close で止まらない")
        # close の中で止まっている間は、記述子も数も残る
        self.assertEqual(self._counts(), (3, 3))
        gate.allow(1)
        with self.assertRaises(IOError):
            s._read_response(num)
        # 例外を出しても、記述子を閉じてから 1 個ぶん戻している（応答が届いた時点で）
        self.assertEqual(self._counts(), (2, 2))
        # paramiko は例外を出したハンドルを表に残す。CLOSE を送り直しても戻さない
        for _ in range(3):
            with self.assertRaises(IOError):
                s._request(CMD_CLOSE, raw)
        self.assertEqual(self._counts(), (2, 2))
        self.assertEqual(list(self.of._held.values()), [2])
        self._assert_ledger_ok(self.ledger)

    def test_a_close_that_raises_before_closing_gives_back_once_closed(self):
        self._close_raises("raise_before")

    def test_a_close_that_raises_after_closing_gives_back_once(self):
        self._close_raises("raise_after")

    def _drop_while_closes_stick(self, drop):
        """a（3 個）を開いたまま切り、後始末の close を 1 個ずつ通す。b は 2 個持つ"""
        a_client = self._connect()
        a = self._session(a_client)
        b = self._session(self._connect())
        held = [a.open("a.txt", "rb") for _ in range(3)]
        held += [b.open("a.txt", "rb") for _ in range(2)]
        self.assertIsNone(self._opens(b), "前提: 全体の上限で断られていない")
        gate = self._gate()
        drop(a_client)
        self.assertTrue(self._wait(lambda: gate.waiting == 1),
                        "前提: 後始末の close で止まらない")
        self.assertEqual(self._counts(), (5, 5))
        self.assertIsNone(self._opens(b), "閉じていない記述子の分で開けた")
        for left in (2, 1, 0):
            gate.allow(1)
            self.assertTrue(self._wait(lambda left=left: self.of._in_use == 2 + left),
                            "数が %d" % self.of._in_use)
            self.assertEqual(fds_on(self.identity), 2 + left)
        time.sleep(0.3)   # paramiko の後始末の 2 回目の close で戻しすぎるなら、ここまでに
        self.assertEqual(self._counts(), (2, 2))
        self.assertEqual(list(self.of._held.values()), [2])
        # 戻った分は b で開ける（b は 1 セッション 3 個まで）
        again = self._opens(b)
        self.assertIsNotNone(again, "閉じた分が戻らない")
        held.append(again)
        self.assertIsNone(self._opens(b))
        self.assertEqual(self._counts(), (3, 3))
        self._assert_ledger_ok(self.ledger)

    def test_a_disconnect_mid_session_gives_back_as_files_close(self):
        self._drop_while_closes_stick(lambda c: c.get_transport().close())

    def test_an_abortive_disconnect_gives_back_as_files_close(self):
        def reset(client):
            """ソケットを RST で切る（SO_LINGER 0 で閉じる）"""
            sock = client.get_transport().sock
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack("hh", 1, 0))
            sock.close()
        self._drop_while_closes_stick(reset)

    def test_stopping_gives_back_as_files_close(self):
        """開いたまま止めて、すぐ起動し直す。新しいセッションは閉じた分だけ開ける。

        前の起動のファイルが残っていれば、起動の診断の行に件数を出す
        """
        diag = "file(s) from earlier sessions still open"

        def diag_lines(out):
            # 比べるのはこの行だけ（ほかの行にはルートのパスなどが入る）
            return [line for line in out.getvalue().splitlines() if diag in line]

        out = io.StringIO()
        self.m.stop()
        with contextlib.redirect_stdout(out):
            self._start(self.m)
        self.assertEqual(diag_lines(out), [], "残っていないのに出した")
        a = self._session(self._connect())
        b = self._session(self._connect())
        held = [a.open("a.txt", "rb") for _ in range(3)]
        held += [b.open("a.txt", "rb") for _ in range(2)]
        gate = self._gate()
        self.m.stop()
        # 止めた後で旧セッションの close を 1 個だけ通す（残りの件数を上限の 5 と
        # 違う値にして、診断の行がどちらを出したかを見分ける）
        gate.allow(1)
        self.assertTrue(self._wait(lambda: self.of._in_use == 4),
                        "数が %d" % self.of._in_use)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._start(self.m)
        # 残りの close はどれも止まっているので、4 個が残っている
        self.assertEqual(
            diag_lines(out),
            ["[SFTP Server] 4 %s, counted toward the limit (5) until closed" % diag])
        # 旧セッションの後始末は、どちらも close で止まっている
        self.assertTrue(self._wait(lambda: gate.waiting == 2),
                        "前提: 後始末の close で止まらない")
        self.assertEqual(self._counts(), (4, 4))
        c = self._session(self._connect())
        d = self._session(self._connect())
        # 閉じた 1 個の分だけ開ける
        again = self._opens(c)
        self.assertIsNotNone(again, "閉じた分が戻らない")
        held.append(again)
        self.assertIsNone(self._opens(c), "閉じていない記述子の分で開けた")
        for k in range(4):
            gate.allow(1)
            self.assertTrue(self._wait(lambda: self.of._in_use == 4),
                            "数が %d" % self.of._in_use)
            self.assertEqual(fds_on(self.identity), 4)
            again = self._opens(c if k < 2 else d)
            self.assertIsNotNone(again, "閉じた分が戻らない")
            held.append(again)
            self.assertEqual(self._counts(), (5, 5))
        time.sleep(0.3)   # paramiko の後始末の 2 回目の close で戻しすぎるなら、ここまでに
        self.assertEqual(self._counts(), (5, 5))
        self.assertEqual(sorted(self.of._held.values()), [2, 3])
        self.assertIsNone(self._opens(d), "全体の上限を超えて開けた")
        self._assert_ledger_ok(self.ledger)


if __name__ == "__main__":
    unittest.main()
