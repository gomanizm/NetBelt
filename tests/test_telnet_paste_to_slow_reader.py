"""読むのが遅い相手へ Telnet で大きく貼り付けても、セッションが切れないことを検証する。

何が起きていたか（基準 028ebc2 で実測）。端末は貼り付けを 512 文字ずつ
key_pressed で渡し、TelnetConnection.send_command は GUI スレッドで
socket.sendall していた。ソケットには受信のために settimeout(0.1) が
掛かっており、OS の送信バッファと相手の受信バッファ（合わせて約 70KB）が
埋まったあと、0.1 秒のうちに空きが出ないと sendall が時間切れになる。
『送信エラー: timed out』として出て切断になり、どこまで送れたかも分からない。
  - localhost（1 秒に約 1,000 バイト読む相手）: 64KB は通り、128KB は
    0.1 秒後に切断。相手が受け取ったのは 131,054 バイト中 3,500 バイト。
  - このファイルの相手（受信バッファ 4096、16KB 読むごとに 0.2 秒休む）へ
    160KB（163,836 バイト）: 約 0.8 秒で『送信エラー: timed out』。端末が
    渡したのは 127,488 文字、そのとき相手が読めていたのは 98,816 バイト。
    直した後は約 2.0 秒で全部が順序どおりに届き、エラーは出ない。
交渉の応答（受信スレッドの _send_telnet_command）も同じソケットへ
sendall で書くので、送信バッファが埋まっているときに応答すると 0.1 秒で
時間切れになり、『Telnet交渉の応答を送信できませんでした: timed out』として
接続を捨てていた。

どう直したか。区切りを待たずに書けるときしか端末に渡させない（背圧）。
TelnetConnection.has_pending_sends は、ソケットへ待たずに書けない
（select で書き込み可にならない）か、交渉の応答を書いている最中なら True を
返し、端末は次の区切りを渡さずに待つ。見張り役（DrainWatcher）が書けるように
なるのを待って send_drained を出す。Windows の TCP は、書き込み可のときの
2KB 程度の send を一度に全部受け取る（実測: 39 回とも 2,048 バイト、部分送信
0 回）ので、send_command の sendall は待たずに終わる。
ソケットへの書き込みは錠で直列化し、交渉の応答がデータの途中へ割り込んで
分かれないようにした。応答は send の戻り値で位置を進め、相手が詰まって
いるだけの時間切れは切断にせず、同じ位置から送り直す。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")

IAC, DO, WONT = 255, 253, 252
ECHO = 1

LINE = ("interface GigabitEthernet0/1\n"
        " description uplink to core.example.com\n"
        " no shutdown\n")


def _paste_body(size):
    """size バイトほどの設定の貼り付け（改行は LF）"""
    return LINE * (size // len(LINE))


def _wire_bytes(body):
    """端末が機器へ送るバイト列（改行は CR）"""
    return body.replace("\n", "\r").encode("utf-8")


class SlowTCPServer:
    """読むのが遅い TCP の相手（localhost）。

    受信バッファを rcvbuf に絞り、burst バイトまで読んだら pause 秒休む。
    reading を clear すると読むのをやめる（機器が詰まった状態）。
    """

    def __init__(self, burst=16384, pause=0.2, rcvbuf=4096):
        self.burst = burst
        self.pause = pause
        self.received = bytearray()
        self.reading = threading.Event()
        self.reading.set()
        self.stop = threading.Event()
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
                data = peer.recv(self.burst)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            self.received.extend(data)
            if self.pause:
                time.sleep(self.pause)

    def close_peer(self):
        """相手側からソケットを閉じる（読んでいない分があるので RST になる）"""
        self.stop.set()
        self.reading.set()
        if self.peer is not None:
            try:
                self.peer.close()
            except OSError:
                pass

    def close(self):
        self.close_peer()
        try:
            self.sock.close()
        except OSError:
            pass


class TelnetPasteToSlowReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _session(self, **server_kwargs):
        """遅い相手へ繋いだ TelnetConnection と、それへ送る端末を返す

        配線は MainWindow と同じ（接続が背圧を持っていれば端末へ渡す）。
        """
        from core.telnet_connection import TelnetConnection
        from ui.terminal_widget import TerminalWidget
        server = SlowTCPServer(**server_kwargs)
        self.addCleanup(server.close)
        conn = TelnetConnection("127.0.0.1", server.port)
        self.addCleanup(conn.dispose)
        self.errors = []
        self.closed = []
        conn.error_occurred.connect(self.errors.append)
        conn.disconnected.connect(lambda: self.closed.append(True))
        # テストのあとに届く知らせが、GC で空にされた lambda を呼ばないよう外す
        self.addCleanup(conn.disconnected.disconnect)
        self.assertTrue(conn.connect(), "前提: localhost の TCP に繋がる")
        deadline = time.time() + 5
        while server.peer is None and time.time() < deadline:
            time.sleep(0.01)

        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        term.key_pressed.connect(conn.send_command)
        backlog = getattr(conn, "has_pending_sends", None)
        drained = getattr(conn, "send_drained", None)
        if backlog is not None and drained is not None:
            term.set_send_backlog(backlog)
            drained.connect(term.resume_send_queue)
        return server, conn, widget, term

    def _pump_until(self, done, seconds):
        end = time.time() + seconds
        while time.time() < end and not done():
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _ticker(self):
        """GUI の短い周期のタイマー。鳴った時刻を控える"""
        from PyQt6.QtCore import QTimer
        stamps = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: stamps.append(time.perf_counter()))
        timer.start()
        self.addCleanup(timer.stop)
        return stamps

    def test_a_large_paste_reaches_a_slow_reader_whole_and_in_order(self):
        server, conn, _, term = self._session()
        body = _paste_body(160 * 1024)
        expected = _wire_bytes(body)
        stamps = self._ticker()

        term.send_text(body)
        self._pump_until(
            lambda: len(server.received) >= len(expected) or self.errors
            or self.closed, 60)

        self.assertEqual([], self.errors, "送信が切断として扱われた")
        self.assertEqual([], self.closed, "セッションが切れた")
        self.assertTrue(conn.is_connected)
        self.assertEqual(len(expected), len(server.received),
                         "相手に届いたバイト数が違う")
        self.assertEqual(expected, bytes(server.received),
                         "届いた順序や中身が違う")
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertGreater(len(stamps), 20, "送信中に GUI のタイマーが進んでいない")
        self.assertLess(max(gaps), 1.0,
                        "送信中に GUI が %.2f 秒止まった" % max(gaps))

    def _stall(self, conn, term):
        """貼り付けを渡し、送信が詰まって見張りが動き出すまで回す

        詰まるかを OS の送信バッファの大きさに任せない。Windows は SO_SNDBUF を
        明示しないソケットの送信バッファを自動で大きくする（動的な送信
        バッファ）ので、環境によっては 192KB を渡しても埋まらない。NetBelt の
        ソケットの SO_SNDBUF を 4096 に明示する（相手の受信バッファは
        SlowTCPServer が 4096 に絞っている）。
        """
        conn.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        term.send_text(_paste_body(192 * 1024))
        self._pump_until(lambda: self.errors or self.closed
                         or self._watcher_thread(conn) is not None, 10)

    @staticmethod
    def _watcher_thread(conn):
        """接続の見張りのスレッド（動いていなければ None）"""
        watcher = getattr(conn, "_drain_watcher", None)
        return watcher._thread if watcher is not None else None

    def test_a_stalled_reader_keeps_the_session_and_the_rest_stays_queued(self):
        """相手が読まなくなっても切らず、渡していない分は端末の列に残ること。"""
        server, conn, _, term = self._session()
        server.reading.clear()

        self._stall(conn, term)
        self._pump_until(lambda: self.errors or self.closed, 0.8)

        self.assertEqual([], self.errors, "相手が詰まっただけで送信エラーになった")
        self.assertEqual([], self.closed)
        self.assertTrue(conn.is_connected)
        self.assertTrue(term._send_queue,
                        "渡していない分が端末の列に残っていない（取り消せない）")

        # 詰まったままでも、後始末で見張りが止まり、固まらない
        thread = self._watcher_thread(conn)
        self.assertIsNotNone(thread, "前提: 送信が詰まり、見張りが動いている")
        started = time.perf_counter()
        conn.dispose()
        self.assertLess(time.perf_counter() - started, 2.5, "後始末が固まった")
        thread.join(1.0)
        self.assertFalse(thread.is_alive(), "見張りのスレッドが残った")
        self._pump_until(lambda: False, 0.2)
        self.assertEqual([], self.errors, "後始末のあとにエラーが出た")

    def test_a_negotiation_reply_waits_for_room_instead_of_disconnecting(self):
        """送信バッファが埋まっていても、交渉の応答は待って送り切り、切断にしないこと。"""
        server, conn, _, term = self._session()
        server.reading.clear()
        # 相手が読まない間に、送信バッファと相手の受信バッファを埋める
        filled = bytearray()
        piece = b"F" * 4096
        while True:
            try:
                sent = conn.socket.send(piece)
            except socket.timeout:
                break
            filled += piece[:sent]
        reply = bytes([IAC, WONT, ECHO])

        replier = threading.Thread(
            target=conn._process_telnet_commands, args=(bytes([IAC, DO, ECHO]),),
            daemon=True)
        replier.start()
        self._pump_until(lambda: self.errors or self.closed, 0.6)

        self.assertEqual([], self.errors, "応答が詰まっただけで切断扱いになった")
        self.assertTrue(conn.is_connected)
        backlog = getattr(conn, "has_pending_sends", None)
        if replier.is_alive() and backlog is not None:
            self.assertTrue(backlog(), "応答を書いている最中に次の区切りを渡させる")

        server.reading.set()
        replier.join(10)
        self.assertFalse(replier.is_alive(), "相手が読んでも応答を書き終えない")
        self._pump_until(
            lambda: len(server.received) >= len(filled) + len(reply), 20)
        self.assertEqual(bytes(filled) + reply, bytes(server.received),
                         "応答が欠けたか、データと混ざった")
        self.assertEqual([], self.errors)

    def test_typed_keys_are_written_at_once(self):
        """打鍵はその場で書き、区切り 1 つ分の待ちを足さないこと。"""
        server, conn, _, term = self._session(pause=0)

        term.send_text("show clock")

        self.assertEqual([], term._send_queue, "打鍵が端末の列に残った")
        self.assertFalse(term._sending, "打鍵のあと送信待ちのまま")
        self._pump_until(lambda: len(server.received) >= 10, 5)
        self.assertEqual(b"show clock", bytes(server.received))

    def test_a_peer_that_closes_while_backlogged_is_reported(self):
        """待っている間に相手が閉じたら、切断として知らせて見張りも終わること。"""
        server, conn, _, term = self._session()
        server.reading.clear()
        self._stall(conn, term)
        self._pump_until(lambda: self.errors or self.closed, 0.5)
        thread = self._watcher_thread(conn)
        self.assertIsNotNone(thread, "前提: 送信が詰まり、見張りが動いている")

        server.close_peer()
        self._pump_until(
            lambda: self.closed or any("送信エラー" in e for e in self.errors), 5)

        self.assertTrue(
            self.closed or any("送信エラー" in e for e in self.errors),
            "相手が閉じたのに知らせが無い: %r" % (self.errors,))
        thread.join(2.0)
        self.assertFalse(thread.is_alive(), "見張りのスレッドが残った")


if __name__ == "__main__":
    unittest.main()
