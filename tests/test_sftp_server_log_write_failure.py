"""内蔵 SFTP サーバーで、診断の行（print）をログへ書けない間も、開けなかった open の
枠を返し、断ったことをパネルへ知らせ、セッションの終わりの後始末と起動・停止を続けること
（Codex レビューの SFTP-01 と、同じ形の start() と stop() の行）。

利用者の決定（2026-10-10）: SFTP-01 と同じ形の弱点を 1.3.4 で直す。

何が起きていたか（8ad283e）: exe は標準出力・標準エラーを行バッファのログファイルへ
向ける（src/main.py の _setup_logging）。保存先の容量不足などで書き込みが失敗する
間は print が例外を出し、その例外が要求の処理と後始末へ波及していた。
- 開けなかった open（無いファイルなど）の枠が返らない。ハンドルが無いので
  セッションの終わりにも戻らず、停止・再起動の後も新しい open を断る。
- 書き込み中の同名への open や、上限で断った open が、パネルのログへ届かない。
- session_ended の close エラーの記録が例外になり、残りのファイルの close と、
  paramiko の finish_subsystem がチャネルを閉じる処理が飛ぶ。
- 前の起動のファイルが残っているときの start() の診断の行が例外になると、
  待ち受けソケットを開いたまま start() から抜け、同じポートで起動し直せない。
- stop() の行（「Stopping server...」など）が例外になると、停止フラグを立てる前に
  stop() から抜け、待ち受けが動き続ける（9fee4af から）。

どう直したか: 診断の行の print の例外は捨てる（標準出力が None のときに print が
黙って捨てるのと同じ扱い）。open は、ハンドルへ移せなかった枠を finally で返す。
session_ended は、記録に失敗しても残りのハンドルを閉じ続ける。

確かめ方: 標準出力・標準エラーを main.py と同じ層（行バッファの UTF-8 テキスト →
バッファ → 書き込みが ENOSPC になる raw）へ差し替える。close の失敗は、容量不足で
書き残しを flush できない書き込みのファイル（close でも flush が失敗して例外になる。
記述子は閉じる）で起こす。セッションの終わりは paramiko の SFTPServer（本物）の
finish_subsystem で流す。start() の行は、その行の print だけを失敗させる。
stop() は、起動の行を書き終えてから書けなくして呼ぶ。
記録の関数そのものが例外を出す形（print 以外の理由）でも、枠の返却と後始末が
続くことを別に確かめる。
"""
import contextlib
import errno
import io
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


def _no_space():
    return OSError(errno.ENOSPC, "No space left on device")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FullDisk(io.RawIOBase):
    """書き込みが容量不足（ENOSPC）で失敗する出力の、いちばん下の層。

    書こうとした中身は attempts へ残す（何を書こうとしたかを確かめる）。
    full を下ろすと書けたことにする（後片付けで閉じるときに使う）
    """

    def __init__(self):
        super().__init__()
        self.full = True
        self.attempts = []

    def writable(self):
        return True

    def write(self, b):
        if self.full:
            self.attempts.append(bytes(b))
            raise _no_space()
        return len(b)


def _exe_like_log(raw):
    """main.py の _setup_logging（open(..., "a", encoding="utf-8", buffering=1)）と
    同じ層の出力。いちばん下の raw だけを差し替える"""
    return io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8",
                            line_buffering=True)


class _FileOnFullDisk(io.FileIO):
    """full を立てると、書き込みが容量不足（ENOSPC）で失敗するファイル"""

    full = False

    def write(self, b):
        if self.full:
            raise _no_space()
        return super().write(b)


class _OsWithFullDisk:
    """core.sftp_server の os の代わり。書き込みで開いたファイルを _FileOnFullDisk で開く。

    作ったファイルは files へ順に残す。容量不足にするのは、試験が選んだファイルだけ
    """

    def __init__(self):
        self.files = []

    def __getattr__(self, name):
        return getattr(os, name)

    def fdopen(self, fd, mode="r", *args, **kwargs):
        if mode != "wb":
            return os.fdopen(fd, mode, *args, **kwargs)
        raw = _FileOnFullDisk(fd, "w")
        self.files.append(raw)
        return io.BufferedWriter(raw)


class _Channel:
    """paramiko の SFTPServer へ渡すチャネル（とトランスポート）の代わり。
    閉じられたかを覚える"""

    def __init__(self):
        self.closed = False

    def get_transport(self):
        return self

    @staticmethod
    def get_log_channel():
        return "paramiko.transport.test-sftp-log-write-failure"

    @staticmethod
    def get_hexdump():
        return False

    def close(self):
        self.closed = True


class _SessionFixture:
    """ルート・数え役と、paramiko の SFTPServer（本物）とそのハンドラを用意する"""

    def _prepare(self):
        import paramiko
        from core import sftp_server
        self.ss = sftp_server
        self.paramiko = paramiko
        self.attr = paramiko.SFTPAttributes()
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-logfail-")
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"abc")
        self.notices = []
        self.open_files = sftp_server._OpenFiles(4, 4)
        self.open_writers = sftp_server._OpenWriters()
        self.channel = _Channel()
        # set_subsystem_handler に渡すのと同じ引数で、paramiko がハンドラを作る
        self.subsystem = paramiko.SFTPServer(
            self.channel, "sftp", None, sftp_server.SFTPServerHandler,
            root_dir=self.root, open_writers=self.open_writers,
            notify=self.notices.append, stop_event=threading.Event(),
            open_files=self.open_files)
        self.handler = self.subsystem.server

    def _counts(self):
        return self.open_files._in_use, self.open_files._held

    def _open_leftovers(self):
        """セッションの終わりに残るファイルを 4 個開く（書き込み 3 個・読み取り 1 個）。

        書き込みの 2 個は、容量不足で書き残しを flush できない状態にする
        （close でも flush が失敗して例外になる。記述子は閉じる）
        """
        disk_os = _OsWithFullDisk()
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        with mock.patch.object(self.ss, "os", disk_os):
            writes = [self.handler.open("/w%d.bin" % i, flags, self.attr)
                      for i in range(3)]
        read = self.handler.open("/a.txt", os.O_RDONLY, self.attr)
        for i, handle in enumerate(writes + [read]):
            self.assertIsInstance(handle, self.paramiko.SFTPHandle)
            # paramiko が OPEN に応えたときに載せる表（finish_subsystem が閉じる）
            self.subsystem.file_table[b"hx%d" % i] = handle
        self.assertEqual(self._counts()[0], 4)
        for f in disk_os.files[:2]:
            f.full = True
        self.assertEqual([h.write(0, b"data") for h in writes],
                         [self.paramiko.SFTP_FAILURE] * 2 + [self.paramiko.SFTP_OK],
                         "前提: 容量不足の書き込みが失敗していない")
        return writes, read

    def _assert_all_closed(self, writes, read):
        self.assertTrue(self.channel.closed, "チャネルを閉じる処理が飛んだ")
        self.assertEqual([h.writefile.closed for h in writes], [True] * 3,
                         "閉じ残した書き込みのファイルがある")
        self.assertTrue(read.readfile.closed, "読み取りのファイルを閉じ残した")
        self.assertEqual(self._counts(), (0, {}))
        self.assertEqual(self.open_writers.alive(), [], "書き込みの一覧に残った")


class SftpServerLogWriteFailureTest(_SessionFixture, unittest.TestCase):
    """標準出力・標準エラー（exe ではログファイル）へ書けない間"""

    def setUp(self):
        self._prepare()
        self.disk = _FullDisk()
        log = _exe_like_log(self.disk)
        self.addCleanup(self._close_log, log)
        # 診断の行は種類ごとに間引くので、窓を新しくして必ず print まで届かせる
        for p in (mock.patch.object(self.ss, "_diag_log", self.ss._LogLimiter()),
                  mock.patch("sys.stdout", log),
                  mock.patch("sys.stderr", log)):
            p.start()
            self.addCleanup(p.stop)

    def _close_log(self, log):
        self.disk.full = False
        log.close()

    def _tried_to_write(self, text):
        return any(text.encode("utf-8") in chunk for chunk in self.disk.attempts)

    def test_a_failed_open_gives_its_slot_back(self):
        """無いファイルの open は SFTP_FAILURE を返し、枠は 0 のまま"""
        for _ in range(self.open_files.total + 1):
            self.assertEqual(
                self.handler.open("/missing.txt", os.O_RDONLY, self.attr),
                self.paramiko.SFTP_FAILURE)
        self.assertTrue(self._tried_to_write("open error"),
                        "前提: 診断の行を書こうとしていない")
        self.assertEqual(self._counts(), (0, {}))
        # 返った枠で、また開ける
        handle = self.handler.open("/a.txt", os.O_RDONLY, self.attr)
        self.assertIsInstance(handle, self.paramiko.SFTPHandle)
        handle.close()
        self.assertEqual(self._counts(), (0, {}))
        self.assertEqual(self.notices, [])

    def test_a_refused_write_open_gives_its_slot_back_and_is_reported(self):
        """書き込み中の同名への 2 本目は SFTP_FAILURE。枠は 1 本目の分だけで、
        断ったことはパネルへ届く"""
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        first = self.handler.open("/w.bin", flags, self.attr)
        self.assertIsInstance(first, self.paramiko.SFTPHandle)
        self.assertEqual(self.handler.open("/w.bin", flags, self.attr),
                         self.paramiko.SFTP_FAILURE)
        self.assertTrue(self._tried_to_write("already open for writing"),
                        "前提: 診断の行を書こうとしていない")
        self.assertEqual(self._counts()[0], 1)
        self.assertEqual(len(self.notices), 1, self.notices)
        self.assertIn("書き込み中", self.notices[0])
        self.assertIn("/w.bin", self.notices[0])
        first.close()
        self.assertEqual(self._counts(), (0, {}))
        # 予約も外れ、同名へまた書ける
        again = self.handler.open("/w.bin", flags, self.attr)
        self.assertIsInstance(again, self.paramiko.SFTPHandle)
        again.close()
        self.assertEqual(self._counts(), (0, {}))

    def test_an_open_over_the_limit_is_reported(self):
        """上限で断った open は SFTP_FAILURE を返し、断ったことはパネルへ届く"""
        per_session = self.open_files.per_session
        held = [self.handler.open("/a.txt", os.O_RDONLY, self.attr)
                for _ in range(per_session)]
        for handle in held:
            self.assertIsInstance(handle, self.paramiko.SFTPHandle)
        self.assertEqual(self.handler.open("/a.txt", os.O_RDONLY, self.attr),
                         self.paramiko.SFTP_FAILURE)
        self.assertTrue(self._tried_to_write("open refused"),
                        "前提: 診断の行を書こうとしていない")
        self.assertEqual(self._counts()[0], per_session)
        self.assertEqual(len(self.notices), 1, self.notices)
        self.assertIn("上限", self.notices[0])
        for handle in held:
            handle.close()
        self.assertEqual(self._counts(), (0, {}))

    def test_session_end_closes_every_file_and_the_channel(self):
        """close が失敗するファイルを含めて、セッションの終わりに全部閉じ、
        枠を全部返し、チャネルも閉じる（finish_subsystem から例外が出ない）"""
        writes, read = self._open_leftovers()
        self.subsystem.finish_subsystem()
        self.assertTrue(self._tried_to_write("session end error"),
                        "前提: close が失敗していないか、記録を書こうとしていない")
        self._assert_all_closed(writes, read)

    def test_flushing_the_omitted_count_does_not_raise(self):
        """間引いた件数を出す flush（停止のとき）も、書けなくても例外を出さない"""
        with mock.patch.object(self.ss._LogLimiter, "LIMIT", 1):
            for i in range(3):
                self.ss._log_limited("probe", "[SFTP Server] probe %d" % i)
            self.ss._diag_log.flush()
        self.assertTrue(self._tried_to_write("probe: 2 more line(s) suppressed"),
                        "前提: 間引いた件数を書こうとしていない")


class SftpServerLogCallFailureTest(_SessionFixture, unittest.TestCase):
    """記録の関数（_log_limited）そのものが例外を出す場合（print 以外の理由）。

    print の例外は _LogLimiter が捨てるので、上の試験は open の finally と
    session_ended の守りを通らない。ここではそれぞれを直接確かめる
    """

    def setUp(self):
        self._prepare()
        self.log_calls = []

        def broken_log(kind, text):
            self.log_calls.append(kind)
            raise RuntimeError("log failed")

        p = mock.patch.object(self.ss, "_log_limited", broken_log)
        p.start()
        self.addCleanup(p.stop)

    def test_a_failed_open_gives_its_slot_back(self):
        # 例外は paramiko が受け、相手へは失敗を返す
        with contextlib.suppress(RuntimeError):
            self.handler.open("/missing.txt", os.O_RDONLY, self.attr)
        self.assertIn("open error", self.log_calls, "前提: 記録を呼んでいない")
        self.assertEqual(self._counts(), (0, {}))

    def test_session_end_closes_every_file_and_the_channel(self):
        writes, read = self._open_leftovers()
        self.subsystem.finish_subsystem()
        self.assertIn("session end error", self.log_calls,
                      "前提: close が失敗していないか、記録を呼んでいない")
        self._assert_all_closed(writes, read)


class SftpServerStartLogFailureTest(unittest.TestCase):
    """前の起動のファイルが残っているときの start() の診断の行だけが書けなくても、
    start() は起動を終える（待ち受けソケットを開いたまま抜けない）"""

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
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-logfail-start-")

    @staticmethod
    def _wait(predicate, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return bool(predicate())

    def test_start_finishes_when_only_the_earlier_sessions_line_fails(self):
        from PyQt6.QtCore import Qt
        from core import sftp_server
        m = sftp_server.SFTPServerManager()
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.host_key = self.host_key
        self.addCleanup(m.stop)
        notices = []
        m.client_activity.connect(
            lambda ip, msg: notices.append(msg),
            Qt.ConnectionType.DirectConnection)
        # 前の起動のファイルが 1 個閉じ終わっていない状態（枠を 1 個埋める）
        slot, _hit = m._open_files.take_slot(object())
        self.assertIsNotNone(slot, "前提: 枠を埋められない")
        self.addCleanup(slot)

        failed = []

        def print_failing_on_that_line(*args, **kwargs):
            line = " ".join(map(str, args))
            if "from earlier sessions" in line:
                failed.append(line)
                raise _no_space()
            print(*args, **kwargs)

        p = mock.patch.object(sftp_server, "print", print_failing_on_that_line,
                              create=True)
        p.start()
        self.addCleanup(p.stop)

        port = free_port()
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        self.assertEqual(len(failed), 1, "前提: その行を書こうとしていない")
        self.assertTrue(self._wait(lambda: m.is_running), "サーバーが起動しない")
        self.assertTrue(
            self._wait(lambda: any("前の接続の終了処理中" in n for n in notices)),
            "パネルのログへの行が届かない: %r" % notices)
        m.stop()
        self.assertEqual(m.server_socket.fileno(), -1, "待ち受けソケットが開いたまま")
        # 同じポートで起動し直せる
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        self.assertTrue(self._wait(lambda: m.is_running), "起動し直せない")


class _DiskThatFills(_FullDisk):
    """書けるところから始まる _FullDisk。full を立てるまでに書けた分は written へ残す"""

    def __init__(self):
        super().__init__()
        self.full = False
        self.written = bytearray()

    def write(self, b):
        count = super().write(b)
        self.written += bytes(b)
        return count


class SftpServerStopLogFailureTest(unittest.TestCase):
    """ログへ書けない状態が続いている間に stop() を呼んでも、停止を最後まで終える。

    元は stop() の最初の行（「Stopping server...」）が例外になり、停止フラグを
    立てる前に抜けて、待ち受けが動き続けた（9fee4af から。7d3922c からは書けない
    間も接続を受け続けるので、この状態のまま停止を押すことが増える）。停止の後も
    残った操作の行と「Server stopped」の行も同じ。ここでは停止の後も残る操作を
    1 つ用意して、stop() の 3 つの行をすべて通す
    """

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
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-logfail-stop-")
        self.disk = _DiskThatFills()
        log = _exe_like_log(self.disk)
        self.addCleanup(self._close_log, log)
        for p in (mock.patch("sys.stdout", log), mock.patch("sys.stderr", log)):
            p.start()
            self.addCleanup(p.stop)

    def _close_log(self, log):
        self.disk.full = False
        log.close()

    def _tried_to_write(self, text):
        return any(text.encode("utf-8") in chunk for chunk in self.disk.attempts)

    _wait = staticmethod(SftpServerStartLogFailureTest._wait)

    def test_stop_finishes_while_the_log_cannot_be_written(self):
        from PyQt6.QtCore import Qt
        from core import sftp_server
        m = sftp_server.SFTPServerManager()
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.WRITER_STOP_TIMEOUT_SECONDS = 0.2
        m.host_key = self.host_key

        def stop_with_the_log_writable():
            self.disk.full = False
            m.stop()

        self.addCleanup(stop_with_the_log_writable)
        stopped = []
        m.stopped.connect(lambda: stopped.append(1),
                          Qt.ConnectionType.DirectConnection)
        port = free_port()
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        # 待受スレッドが起動の行を書き終えてから書けなくする
        self.assertTrue(self._wait(lambda: b"Username: " in self.disk.written),
                        "前提: 起動の行を書き終えない")
        # 停止の後も残る操作（共有フォルダで止まった削除など）を 1 つ用意する
        gate = threading.Event()
        entered = threading.Event()

        def stuck_operation():
            with m._open_writers.busy("/stuck.bin"):
                entered.set()
                gate.wait(30)

        worker = threading.Thread(target=stuck_operation, daemon=True)
        worker.start()
        self.addCleanup(gate.set)
        self.assertTrue(entered.wait(5), "前提: 残る操作を用意できない")

        self.disk.full = True
        self.assertFalse(m.stop(), "停止の後も残った操作を数えていない")
        for line in ("Stopping server...",
                     "still in progress after stop: /stuck.bin",
                     "Server stopped"):
            self.assertTrue(self._tried_to_write(line),
                            "前提: 「%s」を書こうとしていない" % line)
        self.assertFalse(m.server_thread.is_alive(), "待ち受けが止まらない")
        self.assertEqual(m.server_socket.fileno(), -1, "待ち受けソケットが開いたまま")
        self.assertEqual(stopped, [1], "停止の知らせが出ない")
        # 残った操作が終われば、同じポートで起動し直せる（start() の行は
        # 今回の範囲外で、書けないと例外になるので、書けるようにしてから）
        gate.set()
        worker.join(5)
        self.disk.full = False
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        self.assertTrue(self._wait(lambda: m.is_running), "起動し直せない")


if __name__ == "__main__":
    unittest.main()
