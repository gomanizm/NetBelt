"""機器が出力を流し続け、こちら → 機器の TCP が詰まっている間の SSH の後始末で、GUI が止まらないことを検証する。

何が起きていたか（afb8abc の後、localhost で実測）。paramiko の Channel.recv は、
読んだ量がしきい値（受信ウィンドウの 1/10）を超えると WINDOW_ADJUST を
Transport へ書く。こちら → 機器が詰まっていると、受信スレッドはその書き込みで
止まる（スタック: channel.py recv → transport._send_user_message →
packet.py write_all）。SSHConnection.dispose は受信スレッドを
join(timeout=2) で待ってから Transport を閉じていたので、GUI スレッドが
2.0 秒止まった（441ea02 ではそのあとの channel.close も詰まり 5.9 秒）。

どう直したか。受信スレッドはふだん短い眠りの間に抜けるので、まず短く待つ
（SSHConnection._READER_EXIT_WAIT_SECONDS）。それでも抜けなければ Transport を
先に閉じて書き込みを打ち切らせ、そのあとで受信スレッドの終わりを待つ。
書き込みで止まっていた受信スレッドが手にしていた 1 回分（4096 バイトまで）は
渡らないが、待っても詰まりが解けなければ渡らないのは同じ（Transport を
閉じた時点で write_all が EOFError になる）。抜けられるときはこれまでどおり、
手にしている分を渡し終えてから閉じる（記録を欠かさない）。
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

import paramiko                                     # noqa: E402

from test_ssh_dispose_while_send_blocked import (   # noqa: E402
    BODY, _ShellServer, _StallingProxy)

HOLD_SECONDS = 8.0


class _FloodingServer:
    """シェルを開いたら出力を流し続け、入力は読むだけの localhost の相手"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.transport = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            accepted, _ = self.sock.accept()
        except OSError:
            return
        t = paramiko.Transport(accepted)
        self.transport = t
        t.add_server_key(paramiko.ECDSAKey.generate())
        server = _ShellServer()
        try:
            t.start_server(server=server)
        except Exception:
            return
        ch = t.accept(10)
        if ch is None or not server.shell.wait(10):
            return
        self.ready.set()
        line = b"y" * 1000 + b"\r\n"
        while not self.stop.is_set():
            try:
                ch.sendall(line)
            except Exception:
                break

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self.transport is not None:
            self.transport.close()


def _reader_is_writing(conn) -> bool:
    """受信スレッドが Transport への書き込み（WINDOW_ADJUST）で止まっているか"""
    thread = conn._read_thread
    frame = sys._current_frames().get(thread.ident) if thread else None
    while frame is not None:
        if frame.f_code.co_name in ("write_all", "_send_user_message"):
            return True
        frame = frame.f_back
    return False


class DisposeWhileReaderWritesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshdispose-reader-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=d)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def test_dispose_while_the_reader_is_stuck_writing_returns_at_once(self):
        from core.send_backpressure import socket_writable
        from core.ssh_connection import SSHConnection
        from ui.terminal_widget import TerminalWidget
        server = _FloodingServer()
        self.addCleanup(server.close)
        proxy = _StallingProxy(server.port)
        self.addCleanup(proxy.close)
        conn = SSHConnection("127.0.0.1", proxy.port, "u", "p")
        self.addCleanup(conn.dispose)
        self.assertTrue(conn.connect(), "前提: localhost の SSH に繋がる")
        self.assertTrue(server.ready.wait(5))
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        term.key_pressed.connect(conn.send_command)
        term.set_send_backlog(conn.has_pending_sends)
        conn.send_drained.connect(term.resume_send_queue)
        sock = conn.client.get_transport().sock
        # 送信バッファの大きさを OS に任せない（Windows は自動で広げる）
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        proxy.forward.clear()
        # GUI スレッドが止まっても戻せるよう、別スレッドで再開する
        resume = threading.Timer(HOLD_SECONDS, proxy.forward.set)
        resume.start()
        self.addCleanup(resume.cancel)
        term.send_text(BODY)
        self._pump(2, until=lambda: not socket_writable(sock))
        self.assertFalse(socket_writable(sock), "前提: TCP が詰まっていない")
        self._pump(4, until=lambda: _reader_is_writing(conn))
        self.assertTrue(_reader_is_writing(conn),
                        "前提: 受信スレッドが WINDOW_ADJUST の書き込みで止まっていない")

        started = time.perf_counter()
        conn.dispose()
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0,
                        "受信スレッドが書き込みで止まっている間の dispose が"
                        " %.2f 秒止まった" % elapsed)
        self.assertFalse(conn._read_thread.is_alive(),
                         "受信スレッドが止まる前に戻った")
        self.assertIsNone(conn.client)
        self.assertIsNone(conn.channel)


class DisposeOrderTest(unittest.TestCase):
    """受信スレッドの止め方と Transport を閉じる順（偽の受信スレッドで見る）"""

    def _conn(self, client_close):
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "u", "p")
        conn.is_connected = True
        conn.channel = mock.Mock()
        conn.client = mock.Mock()
        conn.client.close.side_effect = client_close
        return conn

    def test_a_reader_held_until_the_transport_closes_does_not_hold_dispose(self):
        closed = threading.Event()
        conn = self._conn(closed.set)
        # Transport が閉じるまで書き込みで止まっている受信スレッドの代わり
        conn._read_thread = threading.Thread(target=closed.wait, args=(10,),
                                             daemon=True)
        conn._read_thread.start()

        started = time.perf_counter()
        conn.dispose()
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0, "dispose が %.2f 秒止まった" % elapsed)
        self.assertTrue(closed.is_set(), "Transport を閉じていない")
        self.assertFalse(conn._read_thread.is_alive(),
                         "受信スレッドが止まる前に戻った")

    def test_a_reader_that_stops_on_its_own_finishes_before_the_transport_closes(self):
        ended = threading.Event()
        reader_alive_at_close = []
        conn = self._conn(
            lambda: reader_alive_at_close.append(not ended.is_set()))

        def reader():
            # 止める印を見てから、手にしている受信を渡し終えるまで少しかかる
            while not conn._stop_reading:
                time.sleep(0.005)
            time.sleep(0.05)
            ended.set()

        conn._read_thread = threading.Thread(target=reader, daemon=True)
        conn._read_thread.start()

        conn.dispose()

        self.assertEqual([False], reader_alive_at_close,
                         "受信スレッドが渡し終える前に Transport を閉じた")


if __name__ == "__main__":
    unittest.main()
