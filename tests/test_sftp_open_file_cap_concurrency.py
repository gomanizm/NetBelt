"""内蔵 SFTP サーバーの開いているファイルの上限（1 セッション 256・全体 2048）を、
並べて開いたときにも超えないこと。

利用者の指示（2026-10-04）: 並行の open で上限を超えないことを確かめる。

tests/test_sftp_open_file_cap.py は、1 本のクライアントが 1 個ずつ応答を待って
開く形だけを見ていた。ここでは次の 2 つを見る。
- _OpenFiles を複数のスレッドから同時に叩く（Barrier で揃える）。製品では
  セッションごとに 1 本のスレッドが要求を順に処理し、セッションをまたいだ数だけが
  スレッドをまたぐ。同じセッションを複数のスレッドから叩く形も、錠の確かめとして
  足す。
- 本物の paramiko のクライアントで、応答を待たずに OPEN を続けて送る（同じ
  セッションへの並行の要求）。複数の接続・セッションから同時にも送る。

開いている間は閉じないので、「認められた数」がその時点で開いている数の最大で
ある。加えて、サーバーの数（_OpenFiles）を別のスレッドから錠の下で見張り、
見えた最大値も上限以下であることを確かめる。
"""
import os
import socket
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
# 応答を待つ上限（秒）。遅い CI と GC の停止（約 0.26 秒）を見込んで長めにする
REPLY_TIMEOUT = 60.0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def pipelined(sftp, requests):
    """要求を応答を待たずに続けて送り、送った順に応答を集める。

    requests は (コマンド, 引数...) の並び。応答ごとに (型, メッセージ) か、
    失敗の状態なら IOError を返す（サーバーは 1 本のセッションの要求を順に
    処理するので、応答は送った順に届く）
    """
    nums = [sftp._async_request(type(None), cmd, *args) for cmd, *args in requests]
    replies = []
    for num in nums:
        try:
            replies.append(sftp._read_response(num))
        except IOError as e:
            replies.append(e)
    return replies


def open_many(sftp, count, path="/a.txt"):
    """a.txt の読み取りの OPEN を count 回続けて送る。(ハンドル, 断られた例外) を返す"""
    from paramiko.sftp import CMD_HANDLE, CMD_OPEN, SFTP_FLAG_READ
    from paramiko.sftp_attr import SFTPAttributes
    replies = pipelined(sftp, [(CMD_OPEN, path, SFTP_FLAG_READ, SFTPAttributes())]
                        * count)
    handles, refused = [], []
    for reply in replies:
        if isinstance(reply, IOError):
            refused.append(reply)
            continue
        t, msg = reply
        if t != CMD_HANDLE:
            raise AssertionError("OPEN の応答が HANDLE でない: %r" % t)
        handles.append(msg.get_binary())
    return handles, refused


def close_many(sftp, handles):
    """ハンドルを続けて閉じる。失敗した CLOSE の数を返す"""
    from paramiko.sftp import CMD_CLOSE
    replies = pipelined(sftp, [(CMD_CLOSE, h) for h in handles])
    return sum(isinstance(r, IOError) for r in replies)


class _Peak:
    """_OpenFiles の数を錠の下で見張り、見えた最大値を覚える"""

    def __init__(self, open_files):
        self._of = open_files
        self.total = 0
        self.session = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            with self._of._lock:
                self.total = max(self.total, self._of._in_use)
                self.session = max([self.session] + list(self._of._held.values()))
            time.sleep(0.001)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(10)


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


class OpenFilesThreadSafetyTest(unittest.TestCase):
    """_OpenFiles を複数のスレッドから同時に叩く。

    錠が無いと「読んでから足す」の間に別のスレッドが割り込み、上限を超えて
    数えるか、数がずれたまま戻らない。割り込みが起きやすいよう、スレッドの
    切り替えの間隔を短くする
    """

    def setUp(self):
        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, interval)

    def test_takes_on_one_session_from_many_threads_stop_at_the_session_limit(self):
        from core.sftp_server import _OpenFiles
        of = _OpenFiles(PER_SESSION, TOTAL)
        session = object()
        threads, each = 8, 40       # 320 回 = 上限 256 + 64
        results = run_together(threads, lambda i: [of.take(session)
                                                   for _ in range(each)])
        flat = [r for rs in results for r in rs]
        self.assertEqual(flat.count(None), PER_SESSION)
        self.assertEqual(len(flat) - flat.count(None), threads * each - PER_SESSION)
        self.assertEqual(set(flat) - {None}, {"session"})
        self.assertEqual((of._in_use, of._held), (PER_SESSION, {session: PER_SESSION}))
        # 同時に返しても、0 へ戻り切る
        run_together(threads, lambda i: [of.give_back(session)
                                         for _ in range(PER_SESSION // threads)])
        self.assertEqual((of._in_use, of._held), (0, {}))

    def test_takes_on_many_sessions_stop_at_the_server_wide_limit(self):
        from core.sftp_server import _OpenFiles
        of = _OpenFiles(PER_SESSION, TOTAL)
        sessions = [object() for _ in range(10)]   # 10 x 256 = 2560 回
        per_thread = PER_SESSION // 2              # 1 セッションを 2 本で叩く

        def take(i):
            session = sessions[i // 2]
            return session, [of.take(session) for _ in range(per_thread)]

        results = run_together(len(sessions) * 2, take)
        granted = {}
        refused = []
        for session, rs in results:
            granted[session] = granted.get(session, 0) + rs.count(None)
            refused += [r for r in rs if r is not None]
        self.assertEqual(sum(granted.values()), TOTAL)
        self.assertEqual(len(refused), len(sessions) * PER_SESSION - TOTAL)
        self.assertEqual(set(refused), {"total"})
        self.assertLessEqual(max(granted.values()), PER_SESSION)
        self.assertEqual(of._in_use, TOTAL)
        self.assertEqual({s: n for s, n in of._held.items()},
                         {s: n for s, n in granted.items() if n})

        # 半分は 1 個ずつ返し、半分はセッションの終わりでまとめて返す
        def release(i):
            session = sessions[i]
            if i % 2:
                of.end(session)
            else:
                for _ in range(granted[session]):
                    of.give_back(session)

        run_together(len(sessions), release)
        self.assertEqual((of._in_use, of._held), (0, {}))

    def test_opens_and_closes_racing_keep_the_counts_within_limits(self):
        from core.sftp_server import _OpenFiles
        # 1 セッションを 3 本、計 18 本で取り合うので、どちらの上限にも当たる
        per_session, total = 2, 10
        of = _OpenFiles(per_session, total)
        sessions = [object() for _ in range(6)]

        def churn(i):
            """開いては閉じるを繰り返し、開いた直後に見えた数の最大を返す"""
            session = sessions[i % len(sessions)]
            peak_total = peak_session = 0
            for _ in range(300):
                if of.take(session) is None:
                    peak_total = max(peak_total, of._in_use)
                    peak_session = max(peak_session, of._held.get(session, 0))
                    of.give_back(session)
            return peak_total, peak_session

        peaks = run_together(len(sessions) * 3, churn)
        self.assertLessEqual(max(p[0] for p in peaks), total)
        self.assertLessEqual(max(p[1] for p in peaks), per_session)
        self.assertEqual((of._in_use, of._held), (0, {}),
                         "取り合いの後で数がずれたまま残った")


class SftpConcurrentOpenTest(unittest.TestCase):
    """本物の paramiko のクライアントで、応答を待たずに並べて開く"""

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
        self.m = self._start()

    def _start(self):
        from core.sftp_server import SFTPServerManager
        m = SFTPServerManager()
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.host_key = self.host_key
        self.addCleanup(self._flush_qt)
        self.addCleanup(m.stop)
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
        """配送待ちの通知（断った知らせなど）を処理しておく"""
        for _ in range(3):
            self.app.processEvents()

    def _session(self, client):
        s = client.open_sftp()
        s.get_channel().settimeout(REPLY_TIMEOUT)
        self.addCleanup(s.close)
        return s

    def _connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                  allow_agent=False, look_for_keys=False, timeout=10)
        self.addCleanup(c.close)
        return c

    @staticmethod
    def _wait(predicate, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return bool(predicate())

    def test_pipelined_opens_on_one_session_stop_at_the_session_limit(self):
        of = self.m._open_files
        s = self._session(self._connect())
        handles, refused = open_many(s, 300)
        self.assertEqual(len(handles), PER_SESSION)
        self.assertEqual(len(refused), 300 - PER_SESSION)
        # 断ったのは SFTP の失敗（無いファイルの ENOENT ではない）
        self.assertTrue(all(e.errno is None for e in refused), refused[:3])
        self.assertEqual(of._in_use, PER_SESSION)
        self.assertEqual(list(of._held.values()), [PER_SESSION])
        self.assertEqual(close_many(s, handles), 0)
        self.assertEqual((of._in_use, of._held), (0, {}))

    def test_opens_from_many_sessions_at_once_stop_at_both_limits(self):
        """3 接続 x 3 セッションから同時に 300 個ずつ（計 2700 個）開く。"""
        of = self.m._open_files
        sessions = [self._session(c) for c in
                    [self._connect() for _ in range(3)] for _ in range(3)]
        each = 300
        with _Peak(of) as peak:
            results = run_together(len(sessions),
                                   lambda i: open_many(sessions[i], each))
        held = [len(handles) for handles, _ in results]
        refused = sum(len(r) for _, r in results)
        self.assertEqual(sum(held), TOTAL, held)
        self.assertEqual(refused, len(sessions) * each - TOTAL)
        self.assertLessEqual(max(held), PER_SESSION, held)
        self.assertLessEqual(peak.total, TOTAL)
        self.assertLessEqual(peak.session, PER_SESSION)
        self.assertEqual(of._in_use, TOTAL)
        self.assertEqual(sorted(of._held.values()), sorted(n for n in held if n))
        # 同時に閉じても 0 へ戻り切る
        failures = run_together(len(sessions),
                                lambda i: close_many(sessions[i], results[i][0]))
        self.assertEqual(failures, [0] * len(sessions))
        self.assertEqual((of._in_use, of._held), (0, {}))


if __name__ == "__main__":
    unittest.main()
