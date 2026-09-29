"""送信の見張り（netbelt-send-drain）を始められなくても、Telnet が切断扱いにせず続きを送り、後始末がソケットを閉じることを検証する。

何が起きていたか（958475e で実測。localhost の読まない TCP の相手へ繋ぎ、
NetBelt 側の SO_SNDBUF を 4096 にして、threading.Thread.start を、名前が
netbelt-send-drain のスレッドだけ RuntimeError にした）。DrainWatcher.check は、
見張りのスレッドを見張り中の印（_thread）に入れてから start していたので、
開始に失敗すると、始まっていないスレッドが印に残ったまま例外が出た
（SSH と同じ弱点。test_ssh_send_watcher_cannot_start.py）。
  - 4096 バイトずつ send_command すると、送信バッファが埋まった 6 回目で
    『送信エラー: can't start new thread』が出た（MainWindow はこれを切断として
    扱い、セッションを閉じる）。そのあと has_pending_sends は、始まっていない
    スレッドが印に残るので True を返し続け、send_drained は出なかった
  - そのあとの dispose は、始まっていないスレッドを join して RuntimeError
    （cannot join thread before it is started）になり、ソケットを閉じずに
    抜けた。相手からは接続が開いたままに見えた（0.5 秒待っても EOF が来ない）
  - 端末（MainWindow と同じ配線）から 192KB を貼り付けると、端末の送信の列の
    has_pending_sends で RuntimeError が出て列が止まり、send_drained も来ない
    ので、相手が読み始めても 196,554 バイト中 12,288 バイトしか届かなかった
    （エラーも出ず、黙って止まったまま）
  - 受信スレッドが交渉の応答を書こうとして見張りを始められないと、
    『読み取りエラー: can't start new thread』で切断になった
DrainWatcher のこの弱点は 441ea02 からある。

どう直したか。DrainWatcher.check は、開始できなかったスレッドを印に残さずに
例外を出し、stop は動いていないスレッドを join しない（SSH と共通）。Telnet は、
見張りを始められないときも「待たずには書けない」として送る列（_out_data・
_out_replies）と端末の列に残し、GUI スレッドのタイマーで（見張りと同じ間隔で。
予約はいつも 1 本だけ）調べ直して、書けるようになったら send_drained を出す。
受信スレッドで失敗したときは、シグナルで GUI スレッドに調べ直しを頼む。
送信エラーとして切断しない。
"""
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

IAC, DO, WONT = 255, 253, 252
ECHO = 1

LINE = ("interface GigabitEthernet0/1\n"
        " description uplink to core.example.com\n"
        " no shutdown\n")

_real_start = threading.Thread.start


class _StuckTCPServer:
    """はじめは読まない TCP の相手（localhost）。reading を set すると読み始める"""

    def __init__(self, rcvbuf=4096):
        self.received = bytearray()
        self.reading = threading.Event()
        self.stop = threading.Event()
        self.closed_seen = threading.Event()   # NetBelt 側が閉じたのを見た
        self.peer = None
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            peer, _ = self.sock.accept()
        except OSError:
            return
        self.peer = peer
        peer.settimeout(0.2)
        while not self.stop.is_set():
            if not self.reading.is_set():
                self.reading.wait(0.05)
                continue
            try:
                data = peer.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                self.closed_seen.set()
                break
            if not data:
                self.closed_seen.set()
                break
            self.received.extend(data)

    def close(self):
        self.stop.set()
        self.reading.set()
        for s in (self.peer, self.sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


class TelnetSendWatcherCannotStartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # アプリの excepthook（main.install_excepthook）と同じく、Qt から
        # 呼ばれた処理の例外を記録して続ける（既定のままだと PyQt が落とす）
        self.hooked = []
        hook = mock.patch.object(
            sys, "excepthook",
            lambda etype, value, tb: self.hooked.append(
                "%s: %s" % (etype.__name__, value)))
        hook.start()
        self.addCleanup(hook.stop)

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _session(self):
        """読まない相手へ繋ぎ、以後は送信の見張りのスレッドを始められないようにする"""
        from core.telnet_connection import TelnetConnection
        server = _StuckTCPServer()
        self.addCleanup(server.close)
        conn = TelnetConnection("127.0.0.1", server.port)
        self.addCleanup(conn.dispose)
        self.errors, self.closed = [], []
        conn.error_occurred.connect(self.errors.append)
        conn.disconnected.connect(lambda: self.closed.append(True))
        # テストのあとに届く知らせが、GC で空にされた lambda を呼ばないよう外す
        self.addCleanup(conn.disconnected.disconnect)
        self.assertTrue(conn.connect(), "前提: localhost の TCP に繋がる")
        deadline = time.time() + 5
        while server.peer is None and time.time() < deadline:
            time.sleep(0.01)
        # 送信バッファの大きさを OS に任せない（Windows は自動で大きくし、
        # そうなると詰まらずに素通りする）
        conn.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

        self.start_failures = 0

        def start(thread):
            if thread.name == "netbelt-send-drain":
                self.start_failures += 1
                raise RuntimeError("can't start new thread")
            return _real_start(thread)
        patcher = mock.patch.object(threading.Thread, "start", start)
        patcher.start()
        self.addCleanup(patcher.stop)
        return server, conn

    def test_a_paste_goes_out_whole_when_the_watcher_cannot_start(self):
        from ui.terminal_widget import TerminalWidget
        server, conn = self._session()
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        term.key_pressed.connect(conn.send_command)
        term.set_send_backlog(conn.has_pending_sends)
        conn.send_drained.connect(term.resume_send_queue)
        body = LINE * (192 * 1024 // len(LINE))
        expected = body.replace("\n", "\r").encode("utf-8")

        raised = []
        try:
            term.send_text(body)
        except RuntimeError as e:
            raised.append(e)
        self._pump(10, until=lambda: self.start_failures or self.errors
                   or self.closed)
        self.assertTrue(self.start_failures,
                        "前提: 送信が詰まり、見張りを始めようとした")
        self._pump(0.5)
        self.assertEqual([], self.errors,
                         "見張りを始められないだけで送信エラー（切断扱い）になった")
        self.assertEqual([], self.closed)

        server.reading.set()                     # 相手が読み始める
        self._pump(60, until=lambda: len(server.received) >= len(expected)
                   or self.errors or self.closed)

        self.assertEqual([], self.errors)
        self.assertEqual([], self.closed)
        self.assertEqual(len(expected), len(server.received),
                         "見張りを始められないと、残りが送られない")
        self.assertEqual(expected, bytes(server.received), "届いた順序や中身が違う")
        self.assertEqual([], raised,
                         "端末からの送信が見張りの開始の失敗をそのまま投げた")
        self.assertEqual([], self.hooked)

    def test_a_negotiation_reply_waits_when_the_watcher_cannot_start(self):
        """受信スレッドが交渉の応答を書けずに見張りを始められなくても、切断しない"""
        server, conn = self._session()
        # 相手が読まない間に、送信バッファと相手の受信バッファを埋める
        filled = bytearray()
        piece = b"F" * 4096
        while True:
            try:
                sent = conn.socket.send(piece)
            except socket.timeout:
                break
            filled += piece[:sent]
        server.peer.sendall(bytes([IAC, DO, ECHO]))   # 受信スレッドが応答する
        self._pump(5, until=lambda: self.start_failures or self.errors
                   or self.closed)
        self.assertTrue(self.start_failures,
                        "前提: 応答が詰まり、見張りを始めようとした")
        self._pump(0.3)
        self.assertEqual([], self.errors,
                         "見張りを始められないだけで切断扱いになった")
        self.assertEqual([], self.closed)
        self.assertTrue(conn.is_connected)

        server.reading.set()
        reply = bytes([IAC, WONT, ECHO])
        self._pump(20, until=lambda: len(server.received) >= len(filled) + len(reply)
                   or self.errors or self.closed)
        self.assertEqual(bytes(filled) + reply, bytes(server.received),
                         "応答が届かないか、データと混ざった")
        self.assertEqual([], self.errors)
        self.assertEqual([], self.hooked)

    def test_dispose_closes_the_socket_after_the_watcher_failed(self):
        server, conn = self._session()
        for _ in range(400):
            try:
                conn.send_command("A" * 4096)
            except RuntimeError:
                pass
            if self.start_failures or self.errors:
                break
        self.assertTrue(self.start_failures,
                        "前提: 送信が詰まり、見張りを始めようとした")

        raised = []
        try:
            conn.dispose()
        except RuntimeError as e:
            raised.append(e)
        server.reading.set()                     # 溜まった分を読んだあと、閉じたのが見える
        self.assertTrue(server.closed_seen.wait(5),
                        "見張りを始められなかったあと、後始末でソケットが閉じない"
                        "（相手から見て接続が残る）")
        self.assertEqual([], raised, "後始末が例外で途中で止まった")
        self._pump(0.3)                          # 予約済みの調べ直しが来る
        self.assertEqual([], self.hooked)


if __name__ == "__main__":
    unittest.main()
