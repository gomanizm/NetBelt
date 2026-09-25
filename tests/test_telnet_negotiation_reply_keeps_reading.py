"""送信が満杯のときに交渉を求められても、Telnet の受信が止まらないことを検証する。

何が起きていたか（fix(telnet) の送信の背圧のあと、基準 87514a3 で実測、
localhost）。交渉の応答（_send_telnet_command）は、受信スレッド（唯一の
読み手）の中で送り切るまで無期限に送り直していた。
  - 相手が読まない間に 256KB を貼り付けて送信バッファを埋め、相手が
    IAC DO ECHO と 1MB の出力を送ってから読み始める（出力を書き終えてから次の
    入力を読む実装）と、8 秒たっても端末へ届いた出力は 0 バイト、相手も送り
    終えられない。接続中の表示のまま、どちらも相手の受信を待って止まった。
    エラーも出ない（38 秒後に後始末するまで応答の書き込みは終わらなかった）。
  修正前（v1.3.0）は 0.1 秒の時間切れで『Telnet交渉の応答を送信できません
  でした』と出して切断していた。

どう直したか。受信スレッドは応答を書くのを待たない。応答は送信の持ち越しと
同じ送る列（応答が先、送信が後）へ積み、錠が取れてソケットへ待たずに書ける
分だけその場で書く。書き残しは見張りが書けるようになったと知らせたときに
GUI スレッドが書く（has_pending_sends / send_drained）。受信はそのまま続ける。
応答は 1 続きで送り（途中へ送信を挟まない）、後から来た貼り付けより先に出す。
書き出せない応答が溜まりすぎたら（相手が読まずに交渉だけを送り続けるなど）、
その間だけ受信を止めてメモリを際限なく使わない。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")

IAC, DO, WILL, WONT, DONT = 255, 253, 251, 252, 254
ECHO, SGA, TTYPE = 1, 3, 24

LINE = ("interface GigabitEthernet0/1\n"
        " description uplink to core.example.com\n"
        " no shutdown\n")


class NonReadingPeer:
    """読まないまま送り続けられる相手（localhost）。

    send_all は、送れた分だけ進める send で本当に送り終えるまで待つ
    （Windows は大きな sendall をカーネルへ丸ごと受け取って即座に返すため）。
    start_reading を呼ぶまで読まない。
    """

    def __init__(self):
        self.received = bytearray()
        self.stop = threading.Event()
        self.reading = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.peer = None

    def accept(self):
        self.sock.settimeout(5)
        self.peer, _ = self.sock.accept()
        self.peer.settimeout(0.2)
        threading.Thread(target=self._read, daemon=True).start()

    def send_all(self, data):
        view = memoryview(data)
        while view and not self.stop.is_set():
            try:
                view = view[self.peer.send(view[:4096]):]
            except socket.timeout:
                continue
            except OSError:
                return

    def start_reading(self):
        self.reading.set()

    def _read(self):
        while not self.stop.is_set():
            if not self.reading.wait(0.05):
                continue
            try:
                data = self.peer.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self.received.extend(data)

    def close(self):
        self.stop.set()
        self.reading.set()
        for sock in (self.peer, self.sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


class TelnetNegotiationReplyKeepsReadingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _session(self):
        """読まない相手へ繋いだ接続と、MainWindow と同じ配線の端末を返す"""
        from core.telnet_connection import TelnetConnection
        from ui.terminal_widget import TerminalWidget
        peer = NonReadingPeer()
        self.addCleanup(peer.close)
        conn = TelnetConnection("127.0.0.1", peer.port)
        self.addCleanup(conn.dispose)
        self.errors, self.closed, self.shown = [], [], []
        conn.error_occurred.connect(self.errors.append)
        conn.disconnected.connect(lambda: self.closed.append(True))
        conn.output_received.connect(self.shown.append)
        # テストのあとに届く知らせが、GC で空にされた lambda を呼ばないよう外す
        self.addCleanup(conn.disconnected.disconnect)
        self.assertTrue(conn.connect(), "前提: localhost の TCP に繋がる")
        # 送信バッファの大きさを OS に任せない。Windows は SO_SNDBUF を明示
        # しないソケットの送信バッファを自動で大きくする（動的な送信バッファ）
        # ので、環境によっては貼り付けや応答を丸ごと受け取り、送信バッファが
        # 埋まらない。4096 に明示すると、待たずに書ける量は数十 KB で止まる
        conn.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        peer.accept()

        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        term.key_pressed.connect(conn.send_command)
        term.set_send_backlog(conn.has_pending_sends)
        conn.send_drained.connect(term.resume_send_queue)
        return peer, conn, term

    def _pump_until(self, done, seconds):
        end = time.time() + seconds
        while time.time() < end and not done():
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _shown_bytes(self):
        return sum(len(text) for text in self.shown)

    def test_a_reply_into_a_full_send_buffer_does_not_stop_receiving(self):
        peer, conn, term = self._session()
        body = LINE * (256 * 1024 // len(LINE))
        wire = body.replace("\n", "\r").encode("utf-8")
        term.send_text(body)
        self._pump_until(lambda: conn.has_pending_sends(), 10)
        self._pump_until(lambda: False, 0.3)
        self.assertTrue(conn.has_pending_sends(), "前提: 送信バッファが埋まる")
        handed = len(wire) - sum(len(entry[0]) for entry in term._send_queue)

        # 相手は出力を書き終えてから次の入力を読む
        output = b"x" * (1024 * 1024)

        def device():
            peer.send_all(bytes([IAC, DO, ECHO]) + output)
            peer.start_reading()

        threading.Thread(target=device, daemon=True).start()
        self._pump_until(
            lambda: self._shown_bytes() >= len(output) or self.errors, 10)

        self.assertEqual(
            len(output), self._shown_bytes(),
            "受信が止まった（交渉の応答を書けずに受信スレッドが待った）")
        self.assertEqual([], self.errors)
        self.assertEqual([], self.closed)
        self.assertTrue(conn.is_connected)

        # 相手が読み始めたら、応答と残りの貼り付けが順序どおり届く
        reply = bytes([IAC, WONT, ECHO])
        self._pump_until(
            lambda: len(peer.received) >= len(wire) + len(reply)
            or self.errors or self.closed, 60)
        self.assertEqual([], self.errors)
        got = bytes(peer.received)
        self.assertEqual(1, got.count(reply), "応答が 1 続きで 1 回届いていない")
        at = got.find(reply)
        self.assertEqual(wire, got[:at] + got[at + len(reply):],
                         "貼り付けが欠けたか、順序が変わった")
        self.assertLessEqual(
            at, handed, "応答が、あとから渡された貼り付けの後ろへ回された")

    def test_replies_at_session_start_are_written_at_once(self):
        """書けるときの応答は受信スレッドがその場で書く（GUI を待たない）。"""
        peer, conn, _ = self._session()
        peer.start_reading()
        peer.send_all(bytes([IAC, DO, TTYPE, IAC, WILL, ECHO, IAC, WILL, SGA])
                      + b"Username: ")
        expected = bytes([IAC, WONT, TTYPE, IAC, DONT, ECHO, IAC, DONT, SGA])

        # イベントを回さない（GUI スレッドが書き出す機会を与えない）
        end = time.time() + 5
        while time.time() < end and len(peer.received) < len(expected):
            time.sleep(0.01)
        self.assertEqual(expected, bytes(peer.received))
        self._pump_until(lambda: "Username: " in "".join(self.shown), 5)
        self.assertEqual("Username: ", "".join(self.shown))
        self.assertEqual([], self.errors)

    def test_piled_up_replies_pause_receiving_and_all_arrive_in_order(self):
        """読まない相手が交渉だけを送り続けても、溜める応答には上限がある。"""
        peer, conn, _ = self._session()
        count = 100000       # 応答 300KB（送信バッファ + 上限を超える）
        threading.Thread(
            target=peer.send_all, args=(bytes([IAC, DO, ECHO]) * count,),
            daemon=True).start()
        limit = conn.MAX_PENDING_REPLIES
        peak = 0
        end = time.time() + 3
        while time.time() < end:
            self.app.processEvents()
            peak = max(peak, len(conn._out_replies))
            time.sleep(0.005)
        self.assertGreater(peak, limit, "前提: 書き出せない応答が上限まで溜まる")
        self.assertLessEqual(peak, limit + 4096, "溜める応答が上限を超えた")
        self.assertTrue(conn.is_connected)

        peer.start_reading()
        self._pump_until(
            lambda: len(peer.received) >= 3 * count or self.errors, 30)
        self.assertEqual([], self.errors)
        self.assertEqual(bytes([IAC, WONT, ECHO]) * count, bytes(peer.received))

    def test_queued_replies_are_not_written_to_the_next_connection(self):
        """切断したら積んだ応答は捨て、繋ぎ直した先へ書かない。"""
        peer, conn, _ = self._session()
        threading.Thread(
            target=peer.send_all, args=(bytes([IAC, DO, ECHO]) * 30000,),
            daemon=True).start()
        self._pump_until(lambda: len(conn._out_replies) > 0, 10)
        self.assertGreater(len(conn._out_replies), 0, "前提: 応答が積まれている")

        conn.disconnect()
        peer.close()
        second = NonReadingPeer()
        self.addCleanup(second.close)
        conn.port = second.port
        self.assertTrue(conn.connect())
        second.accept()
        second.start_reading()
        conn.send_command("show clock\r")
        self._pump_until(lambda: len(second.received) >= 11, 5)
        self._pump_until(lambda: False, 0.3)
        self.assertEqual(b"show clock\r", bytes(second.received),
                         "前の接続の応答が新しい接続へ流れた")


if __name__ == "__main__":
    unittest.main()
