"""SSH の受信スレッドが WINDOW_ADJUST の書き込みで止まっている最中に後始末しても、読み終えた受信が渡ることを検証する。

何が起きていたか（63f67e4、localhost で実測）。機器が出力を流し続け、こちら →
機器の TCP が詰まっていると、paramiko の Channel.recv は読んだ分を手にしたまま
WINDOW_ADJUST の書き込みで止まる（channel.py recv → transport._send_user_message →
packet.py write_all）。後始末（SSHConnection.dispose）は GUI を止めないために
Transport を先に閉じる（afb8abc・d910c8a）。すると書き込みが EOFError になり、
recv が読み終えていた分（4096 バイトまで）は output_received に出ないまま
捨てられた。タブを閉じる・切断・ウィンドウを閉じる経路では、この分が
セッションの記録から欠けた（取り出した 1,057,184 バイトのうち 4,096 バイトが
渡らなかった）。基準 441ea02 は、詰まりが解けるまで GUI を止めて待ち、解ければ
渡していた（解けなければ戻らなかった）。

どう直したか。受信スレッドは Channel.recv と同じ手順で読むが、読んだ分を
output_received で渡してから WINDOW_ADJUST を書く（SSHConnection._recv_handing_over）。
書き込みで止まったまま後始末されても、読み終えた分は渡し済みになる。
"""
import itertools
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
from PyQt6.QtCore import Qt                         # noqa: E402

from test_ssh_dispose_while_send_blocked import (   # noqa: E402
    BODY, _ShellServer, _StallingProxy)
from test_ssh_dispose_while_reader_writes import _reader_is_writing  # noqa: E402

LINE_WIDTH = 12


def _stream_line(i: int) -> bytes:
    """機器が流す i 行目（10 桁の通し番号と CRLF。どこで切れても続きが分かる）"""
    return b"%010d\r\n" % i


def _stream_prefix(length: int) -> bytes:
    lines = (_stream_line(i) for i in itertools.count())
    return b"".join(itertools.islice(lines, length // LINE_WIDTH + 1))[:length]


class _CountingFloodServer:
    """シェルを開いたら通し番号の行を流し続け、入力は読むだけの localhost の相手"""

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
        for start in itertools.count(0, 100):
            if self.stop.is_set():
                break
            try:
                ch.sendall(b"".join(_stream_line(i)
                                    for i in range(start, start + 100)))
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


class _RecordingTransport:
    """Channel が Transport へ書くメッセージを記録するだけの偽物"""

    def __init__(self, events, fail=False):
        self.events = events
        self.fail = fail
        self.messages = []

    def _send_user_message(self, m):
        self.events.append("window_adjust")
        self.messages.append(m.asbytes())
        if self.fail:
            # Transport が閉じられたときの packet.py write_all と同じ
            raise EOFError()


class ReaderHandsOverBeforeWindowAdjustTest(unittest.TestCase):
    """本物の paramiko.Channel に偽の Transport をつないで、受信スレッドを 1 周回す"""

    DATA = b"show running-config\r\n" * 10     # しきい値（100）を超える量

    def _channel(self, transport):
        ch = paramiko.Channel(7)
        ch.remote_chanid = 42
        ch.active = True
        ch.transport = transport
        ch.in_window_threshold = 100
        ch.in_buffer.feed(self.DATA)
        self.addCleanup(setattr, ch, "active", False)
        return ch

    def _run_reader(self, channel, events, stop_on_write=False):
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.channel = channel
        conn.is_connected = True
        out = []

        def on_output(text):
            events.append("output")
            out.append(text)
        conn.output_received.connect(on_output)
        if stop_on_write:
            # 書いたら受信ループを止める（本物なら dispose が止める）
            send = channel.transport._send_user_message

            def send_and_stop(m):
                conn._stop_reading = True
                send(m)
            channel.transport._send_user_message = send_and_stop
        conn._read_output()
        return "".join(out)

    def test_a_chunk_read_before_a_failed_window_adjust_is_handed_over(self):
        events = []
        channel = self._channel(_RecordingTransport(events, fail=True))

        out = self._run_reader(channel, events)

        self.assertEqual(self.DATA.decode(), out,
                         "WINDOW_ADJUST の書き込みが失敗すると、読み終えた受信が渡らない")

    def test_the_chunk_is_handed_over_before_the_window_adjust_is_written(self):
        events = []
        channel = self._channel(_RecordingTransport(events))

        self._run_reader(channel, events, stop_on_write=True)

        self.assertEqual(["output", "window_adjust"], events)

    def test_the_window_adjust_is_the_one_channel_recv_would_send(self):
        reference_events = []
        reference = self._channel(_RecordingTransport(reference_events))
        self.assertEqual(self.DATA, reference.recv(4096), "前提: paramiko の recv")
        events = []
        channel = self._channel(_RecordingTransport(events))

        self._run_reader(channel, events, stop_on_write=True)

        self.assertEqual(reference.transport.messages,
                         channel.transport.messages,
                         "Channel.recv と違う WINDOW_ADJUST を書いた")
        self.assertEqual(0, channel.in_window_sofar)

    def test_below_the_threshold_nothing_is_written(self):
        events = []
        channel = self._channel(_RecordingTransport(events))
        channel.in_window_threshold = 10 * len(self.DATA)
        channel.closed = False

        def stop_when_drained():
            # 読み終えたら受信ループを止める（本物なら dispose が止める）
            ready = channel.in_buffer.read_ready()
            if not ready:
                channel.closed = True
            return ready
        channel.recv_ready = stop_when_drained

        out = self._run_reader(channel, events)

        self.assertEqual(self.DATA.decode(), out)
        self.assertEqual([], channel.transport.messages)
        self.assertEqual(len(self.DATA), channel.in_window_sofar)


class DisposeWhileReaderWritesKeepsTheChunkTest(unittest.TestCase):
    """機器が流し続け、こちら → 機器が詰まっている間に dispose する（localhost）"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshdispose-chunk-"))
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

    def test_what_the_stuck_reader_took_is_delivered(self):
        from core.send_backpressure import socket_writable
        from core.ssh_connection import SSHConnection
        from ui.terminal_widget import TerminalWidget
        server = _CountingFloodServer()
        self.addCleanup(server.close)
        proxy = _StallingProxy(server.port)
        self.addCleanup(proxy.close)
        conn = SSHConnection("127.0.0.1", proxy.port, "u", "p")
        self.addCleanup(conn.dispose)
        delivered = []
        # 受信スレッドの中で受け取る（GUI のイベントループを待たない）
        conn.output_received.connect(
            delivered.append, type=Qt.ConnectionType.DirectConnection)
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
        # 後始末のあとで再開する（詰まりが解けても渡るかどうかは変わらない）
        resume = threading.Timer(8.0, proxy.forward.set)
        resume.start()
        self.addCleanup(resume.cancel)
        term.send_text(BODY)
        self._pump(2, until=lambda: not socket_writable(sock))
        self.assertFalse(socket_writable(sock), "前提: TCP が詰まっていない")
        self._pump(4, until=lambda: _reader_is_writing(conn))
        self.assertTrue(_reader_is_writing(conn),
                        "前提: 受信スレッドが WINDOW_ADJUST の書き込みで止まっていない")
        channel = conn.channel

        started = time.perf_counter()
        conn.dispose()
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0, "dispose が %.2f 秒止まった" % elapsed)
        self.assertFalse(conn._read_thread.is_alive(),
                         "受信スレッドが止まる前に戻った")
        # 渡した分と、まだ読んでいない分を繋ぐと、機器が流した列の先頭と
        # 一致するはず。受信スレッドが手にしていた分を捨てると、間が抜ける
        got = "".join(delivered).encode("ascii") + channel.in_buffer.empty()
        self.assertGreater(len(got), 100 * LINE_WIDTH, "前提: 受信が流れていない")
        expected = _stream_prefix(len(got))
        if got != expected:
            at = next(i for i, (a, b) in enumerate(zip(got, expected)) if a != b)
            self.fail("記録に渡った受信が %d バイト目から抜けている"
                      "（受け取った %d バイト、%r の前後）"
                      % (at, len(got), expected[at - 12:at + 12]))


if __name__ == "__main__":
    unittest.main()
