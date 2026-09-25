"""MainWindow の貼り付けで、読むのが遅い機器へ大きく送ってもセッションが切れないことを検証する。

何が起きていたか（基準 028ebc2 で実測）。右クリックの貼り付け（確認ダイアログで
送信）は端末の送信列を通り、512 文字ずつ SSH / Telnet の send_command へ
渡る。接続は GUI スレッドで sendall し、受信のための 0.1 秒の時間切れで
失敗すると『送信エラー: …』を出す。MainWindow._on_connection_error は
これを切断として扱い、接続を捨てて再接続待ちに入る。
  - 利用者の実機（CML の IOSv へ SSH、2026-09-24）: 16KB のコメント行だけの
    ファイル（219 行）を貼り付けると切れ、機器に届いたのは 111 行目まで。
  - 検査役の localhost（1 秒に約 1,000 バイト読む相手）: Telnet は 128KB で
    0.1 秒後に『送信エラー: timed out』、SSH（ウィンドウ 32768）は 48KB で
    0.11 秒後に『送信エラー: 』。どちらもセッションが切れた。

どう直したか。SSH・Telnet の接続に、シリアルと同じ背圧の口
（has_pending_sends / send_drained）を持たせた（前の 2 コミット）。ここでは
MainWindow が SSH・Telnet の端末にも set_send_backlog と resume_send_queue を
配線する。端末は接続が待たずに書けるときだけ次の区切りを渡すので、送信は
時間切れにならず、未送信の分は端末の列に残ってマクロの停止で取り消せる。
切断・タブを閉じる・再接続待ちでは、見張りのスレッドが止まり、端末の列は
再接続待ちで捨てられるので、古い残りが新しい接続へ流れない。
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
    """読むのが遅い TCP の相手（localhost）。接続ごとに受け取った分を控える。

    受信バッファを 4096 に絞り、burst バイトまで読んだら pause 秒休む。
    reading を clear すると、どの接続も読むのをやめる（機器が詰まった状態）。
    """

    def __init__(self, burst=16384, pause=0.2):
        self.burst = burst
        self.pause = pause
        self.sessions = []     # 接続ごとの受け取った分（bytearray）
        self.peers = []
        self.reading = threading.Event()
        self.reading.set()
        self.stop = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                peer, _ = self.sock.accept()
            except OSError:
                return
            received = bytearray()
            self.peers.append(peer)
            self.sessions.append(received)
            threading.Thread(target=self._serve, args=(peer, received),
                             daemon=True).start()

    def _serve(self, peer, received):
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
            received.extend(data)
            if self.pause:
                time.sleep(self.pause)

    def close_peers(self):
        """相手側から閉じる（読んでいない分があるので RST になる）"""
        for peer in self.peers:
            try:
                peer.close()
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.reading.set()
        self.close_peers()
        try:
            self.sock.close()
        except OSError:
            pass


class _ShellServer(paramiko.ServerInterface):
    def __init__(self):
        self.shell = threading.Event()

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_pty_request(self, channel, term, width, height,
                                  pixelwidth, pixelheight, modes):
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True

    def check_channel_window_change_request(self, channel, width, height,
                                            pixelwidth, pixelheight):
        return True


class SlowSSHServer:
    """読むのが遅い SSH サーバ（localhost）。ウィンドウを 16384 に絞る"""

    def __init__(self, burst=16384, pause=0.25):
        self.key = paramiko.ECDSAKey.generate()
        self.burst = burst
        self.pause = pause
        self.received = bytearray()
        self.stop = threading.Event()
        self.channel = None
        self.transport = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            accepted, _ = self.sock.accept()
        except OSError:
            return
        transport = paramiko.Transport(accepted)
        self.transport = transport
        transport.default_window_size = 16384
        transport.add_server_key(self.key)
        server = _ShellServer()
        try:
            transport.start_server(server=server)
        except Exception:
            return
        channel = transport.accept(10)
        if channel is None or not server.shell.wait(10):
            transport.close()
            return
        self.channel = channel
        channel.settimeout(0.2)
        while not self.stop.is_set():
            try:
                data = channel.recv(self.burst)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data:
                break
            self.received.extend(data)
            time.sleep(self.pause)

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self.transport is not None:
            self.transport.close()


class PasteToSlowDeviceKeepsSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = Path(tempfile.mkdtemp(prefix="netbelt-slowpaste-"))
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(d / "config.json"))),
                # SFTP はこの検証に関係ない（相手のサーバも答えない）
                mock.patch.object(MainWindow, "_start_sftp_session")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = MainWindow()
        self.addCleanup(self._close_window)

    def _close_window(self):
        self.window.macro_manager.cleanup_device("dev")
        self.window.close()
        self._pump(0.1)

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _connect(self, protocol, port):
        device = {"name": "dev", "host": "127.0.0.1", "port": port,
                  "protocol": protocol, "username": "u", "password": "p"}
        if protocol == "telnet":
            self.window._connect_telnet(device)
        else:
            self.window._connect_ssh(device)
        self._pump(10, until=lambda: self._terminal().can_send_input())
        self.assertTrue(self._terminal().can_send_input(), "前提: 接続できている")
        return self.window.connections["dev"]

    def _terminal(self):
        return self.window.terminal_widget._terminals["dev"]

    def _paste(self, text):
        """右クリックの貼り付け（改行を含むので確認ダイアログが出る。送信を選ぶ）"""
        from PyQt6.QtWidgets import QApplication, QDialog
        import ui.dialogs.paste_confirm_dialog as confirm
        QApplication.clipboard().setText(text)
        with mock.patch.object(confirm.PasteConfirmDialog, "exec",
                               return_value=QDialog.DialogCode.Accepted):
            self._terminal().custom_paste()

    def _ticker(self):
        from PyQt6.QtCore import QTimer
        stamps = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: stamps.append(time.perf_counter()))
        timer.start()
        self.addCleanup(timer.stop)
        return stamps

    def _still_connected(self, conn):
        return (self.window.connections.get("dev") is conn
                and not self._terminal()._reconnect_mode)

    def _assert_arrived_whole(self, conn, received, expected, stamps):
        self.assertTrue(self._still_connected(conn),
                        "セッションが切れた: %s"
                        % self.window.status_bar.currentMessage())
        self.assertEqual(len(expected), len(received), "相手に届いたバイト数が違う")
        self.assertEqual(expected, bytes(received), "届いた順序や中身が違う")
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertGreater(len(stamps), 20, "送信中に GUI のタイマーが進んでいない")
        self.assertLess(max(gaps), 1.0,
                        "送信中に GUI が %.2f 秒止まった" % max(gaps))

    def test_ssh_paste_to_a_slow_reader_arrives_whole(self):
        server = SlowSSHServer()
        self.addCleanup(server.close)
        conn = self._connect("ssh", server.port)
        expected = _wire_bytes(_paste_body(128 * 1024))
        stamps = self._ticker()

        self._paste(_paste_body(128 * 1024))
        self._pump(60, until=lambda: len(server.received) >= len(expected)
                   or not self._still_connected(conn))

        self._assert_arrived_whole(conn, server.received, expected, stamps)

    def test_telnet_paste_to_a_slow_reader_arrives_whole(self):
        server = SlowTCPServer()
        self.addCleanup(server.close)
        conn = self._connect("telnet", server.port)
        # 送信バッファの大きさを OS に任せない（Windows は自動で大きくし、そうなると
        # 詰まらずに素通りする。GitHub のランナーで前提が崩れた）
        conn.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        expected = _wire_bytes(_paste_body(160 * 1024))
        stamps = self._ticker()

        self._paste(_paste_body(160 * 1024))
        self._pump(60, until=lambda: len(server.sessions[0]) >= len(expected)
                   or not self._still_connected(conn))

        self._assert_arrived_whole(conn, server.sessions[0], expected, stamps)

    def _stalled_paste(self):
        """読まない相手へ Telnet で繋ぎ、貼り付けが詰まった状態にする

        詰まるか（見張りが動き出すか）を OS の送信バッファの大きさに任せない。
        Windows は SO_SNDBUF を明示しないソケットの送信バッファを自動で
        大きくする（動的な送信バッファ）ので、CI（windows-latest）では 192KB を
        0.6 秒渡しても埋まらず、見張りが動かないまま進んでいた。
        NetBelt のソケットの SO_SNDBUF を 4096 に明示し（相手の受信バッファは
        SlowTCPServer が 4096 に絞っている）、見張りが動き出すまで待つ。
        """
        server = SlowTCPServer()
        self.addCleanup(server.close)
        conn = self._connect("telnet", server.port)
        conn.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        server.reading.clear()
        body = _paste_body(192 * 1024)
        self._paste(body)
        self._pump(10, until=lambda: not self._still_connected(conn)
                   or self._watcher_thread(conn) is not None)
        # 詰まったあとも、時間切れで切断にならないことを確かめる
        self._pump(0.6, until=lambda: not self._still_connected(conn))
        self.assertTrue(self._still_connected(conn),
                        "相手が詰まっただけでセッションが切れた: %s"
                        % self.window.status_bar.currentMessage())
        self.assertTrue(self._terminal()._send_queue,
                        "前提: 渡していない分が端末の列に残っている")
        thread = self._watcher_thread(conn)
        self.assertIsNotNone(thread, "前提: 送信が詰まり、見張りが動いている")
        return server, conn, body, thread

    @staticmethod
    def _watcher_thread(conn):
        """接続の見張りのスレッド（動いていなければ None）"""
        watcher = getattr(conn, "_drain_watcher", None)
        return watcher._thread if watcher is not None else None

    def test_stopping_a_macro_queued_behind_a_stalled_paste_sends_none_of_it(self):
        server, conn, body, _ = self._stalled_paste()
        manager = self.window.macro_manager
        manager.start_command_list("dev", ["show clock", "show users"], 100)
        self._pump(0.3)

        manager.stop_command_list("dev")
        server.reading.set()
        expected = _wire_bytes(body)
        self._pump(60, until=lambda: len(server.sessions[0]) >= len(expected)
                   or not self._still_connected(conn))
        self._pump(0.5)

        received = bytes(server.sessions[0])
        self.assertTrue(self._still_connected(conn))
        self.assertNotIn(b"show clock", received, "止めたマクロの行が届いた")
        self.assertNotIn(b"show users", received, "止めたマクロの行が届いた")
        self.assertEqual(expected, received, "貼り付けが欠けたか順序が違う")

    def test_closing_the_tab_while_stalled_stops_the_watcher(self):
        server, conn, _, thread = self._stalled_paste()
        widget = self.window.terminal_widget
        index = [widget.tab_widget.tabText(i)
                 for i in range(widget.tab_widget.count())].index("dev")

        started = time.perf_counter()
        widget._close_tab(index)
        self.assertLess(time.perf_counter() - started, 3.0, "タブを閉じるのが固まった")

        self.assertNotIn("dev", self.window.connections)
        self.assertIsNotNone(thread, "前提: 見張りが動いていた")
        thread.join(1.0)
        self.assertFalse(thread.is_alive(), "見張りのスレッドが残った")

    def test_reconnecting_after_a_stalled_disconnect_sends_nothing_old(self):
        server, conn, _, thread = self._stalled_paste()

        # 「切断」ボタンと同じ後始末（タブは残して再接続待ちに入る）
        self.window._on_tab_closed("dev")
        self._pump(0.2)
        self.assertTrue(self._terminal()._reconnect_mode, "前提: 再接続待ち")
        self.assertIsNotNone(thread, "前提: 見張りが動いていた")
        thread.join(1.0)
        self.assertFalse(thread.is_alive(), "見張りのスレッドが残った")

        server.reading.set()
        self.window._reconnect_device("dev")
        self._pump(10, until=lambda: len(server.sessions) >= 2
                   and self._terminal().can_send_input())
        self.assertTrue(self._terminal().can_send_input(), "前提: 繋ぎ直せた")
        self._pump(0.8)
        self.assertEqual(b"", bytes(server.sessions[1]),
                         "前の接続の貼り付けの残りが新しい接続へ流れた")

        self._terminal().send_text("x")
        self._pump(5, until=lambda: len(server.sessions[1]) >= 1)
        self._pump(0.3)
        self.assertEqual(b"x", bytes(server.sessions[1]))

    def test_a_peer_that_closes_during_a_stalled_paste_ends_the_session(self):
        server, conn, _, thread = self._stalled_paste()

        server.close_peers()
        self._pump(5, until=lambda: not self._still_connected(conn))

        self.assertNotIn("dev", self.window.connections, "切断として扱われていない")
        self.assertTrue(self._terminal()._reconnect_mode, "再接続待ちに入っていない")
        self.assertEqual([], self._terminal()._send_queue,
                         "切れた接続宛ての残りを捨てていない")
        if thread is not None:
            thread.join(2.0)
            self.assertFalse(thread.is_alive(), "見張りのスレッドが残った")


if __name__ == "__main__":
    unittest.main()
