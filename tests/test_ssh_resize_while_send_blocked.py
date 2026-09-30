"""SSH で相手が TCP まで受け取りを止めている間に端末の大きさが変わっても、GUI が止まらないことを検証する。

何が起きていたか（基準 441ea02 で実測、localhost）。構成はクライアント →
中継 → paramiko のシェルサーバ（受信ウィンドウ 2MB）。中継が転送を止めて
TCP を詰まらせ、205KB を貼り付ける。詰まってから 1 秒後に
set_terminal_size(100, 30) を呼ぶと 7.0 秒戻らず、中継を戻すまで GUI
スレッドが止まった（GUI の最大の止まりも 7.0 秒）。詰まっている間の送信
そのものは背圧で GUI を止めない（最大 0.02 秒）。
set_terminal_size は GUI スレッド（窓の大きさ・フォント・一覧の表示切替など、
端末の大きさが変わる操作すべて）から channel.resize_pty を呼ぶ。paramiko は
window-change を Transport へ書くとき、書けるまで時間切れを無限に再試行する
ので、相手が受け取りを止めている間は戻らない。相手が生きたまま受け取らない
（ゼロウィンドウ）なら無期限に止まる。

どう直したか。Transport へいま書くと待たされる（鍵交換中・TCP へ書けない）
間は window-change を送らず、大きさだけを覚えて見張り（DrainWatcher）を
起こしておく。Transport へ書けるようになったら、覚えておいた最後の大きさを
1 回だけ送る。チャネルの窓は見ない（window-change は窓と関係なく書ける）
ので、窓が 0 なだけのときは今までどおりその場で送る。窓が 0 のまま詰まりが
解けた場合も、窓が空くのを待たずに送る
（tests/test_ssh_resize_after_transport_recovers.py）。
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
BODY = LINE * 2500     # 約 205KB


def _stuffed_socketpair():
    """送る側（1 つ目）の送信バッファを埋めきった、待たずに書けないソケットの組"""
    a, b = socket.socketpair()
    # Windows は SO_SNDBUF を明示しないと送信バッファを自動で広げる
    a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    b.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    a.setblocking(False)
    try:
        while True:
            a.send(b"x" * 65536)
    except BlockingIOError:
        pass
    return a, b


class _FakeTransport:
    def __init__(self, sock):
        self.sock = sock
        self.clear_to_send = threading.Event()
        self.clear_to_send.set()


class _FakeChannel:
    """resize_pty の呼び出しだけを記録するチャネル（偽物）"""

    closed = False
    out_window_size = 1024 * 1024

    def __init__(self, transport):
        self.transport = transport
        self.resizes = []

    def get_transport(self):
        return self.transport

    def resize_pty(self, width, height):
        self.resizes.append((width, height))


class _ShellServer(paramiko.ServerInterface):
    def __init__(self, resizes):
        self.shell = threading.Event()
        self.resizes = resizes

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True

    def check_channel_window_change_request(self, channel, width, height,
                                            pixelwidth, pixelheight):
        self.resizes.append((width, height))
        return True


class _SSHServer:
    """受信したものを溜める localhost のシェル（受信ウィンドウ 2MB）"""

    def __init__(self):
        self.received = bytearray()
        self.resizes = []
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
        t.default_window_size = 2 * 1024 * 1024
        t.add_server_key(paramiko.ECDSAKey.generate())
        server = _ShellServer(self.resizes)
        try:
            t.start_server(server=server)
        except Exception:
            return
        ch = t.accept(10)
        if ch is None or not server.shell.wait(10):
            return
        ch.settimeout(0.2)
        self.ready.set()
        while not self.stop.is_set():
            try:
                d = ch.recv(65536)
            except socket.timeout:
                continue
            except Exception:
                break
            if not d:
                break
            self.received.extend(d)

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self.transport is not None:
            self.transport.close()


class _StallingProxy:
    """クライアント → サーバの転送を forward で止められる中継（受信バッファは小さく）"""

    def __init__(self, target_port):
        self.target_port = target_port
        self.forward = threading.Event()
        self.forward.set()
        self.stop = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.links = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _pump(self, src, dst, gated):
        src.settimeout(0.1)
        while not self.stop.is_set():
            if gated and not self.forward.is_set():
                self.forward.wait(0.05)
                continue
            try:
                d = src.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not d:
                break
            try:
                dst.sendall(d)
            except OSError:
                break
        # 片側が閉じたら、もう片側へも閉じたことを伝える
        for s in (src, dst):
            try:
                s.close()
            except OSError:
                pass

    def _serve(self):
        try:
            client, _ = self.sock.accept()
        except OSError:
            return
        server = socket.create_connection(("127.0.0.1", self.target_port))
        self.links = [client, server]
        threading.Thread(target=self._pump, args=(client, server, True),
                         daemon=True).start()
        threading.Thread(target=self._pump, args=(server, client, False),
                         daemon=True).start()

    def close(self):
        self.stop.set()
        self.forward.set()
        for s in [self.sock] + self.links:
            try:
                s.close()
            except OSError:
                pass


class SSHResizeWhileSendBlockedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshresize-"))
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

    def _fake_session(self):
        """接続済みと同じ見張りを持つ SSHConnection に、偽のチャネルを差す"""
        from core.send_backpressure import DrainWatcher
        from core.ssh_connection import SSHConnection
        a, b = _stuffed_socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        channel = _FakeChannel(_FakeTransport(a))
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.channel = channel
        conn.is_connected = True
        conn._drain_watcher = DrainWatcher(
            lambda: conn._send_backlogged(channel), conn._announce_drained)
        self.addCleanup(conn._drain_watcher.stop)
        return conn, channel, b

    @staticmethod
    def _drain(peer):
        """相手側で溜まった分を読み、送る側が書けるようにする"""
        peer.setblocking(False)
        try:
            while True:
                if not peer.recv(65536):
                    break
        except (BlockingIOError, OSError):
            pass

    def test_a_resize_waits_while_the_transport_cannot_be_written(self):
        conn, channel, _ = self._fake_session()

        conn.set_terminal_size(100, 30)

        self.assertEqual([], channel.resizes,
                         "TCP へ書けない間に window-change を書いた"
                         "（paramiko は書けるまで GUI スレッドで待つ）")
        self.assertEqual((100, 30), (conn.term_cols, conn.term_rows))

    def test_the_last_size_goes_out_once_writing_is_possible_again(self):
        conn, channel, peer = self._fake_session()
        conn.set_terminal_size(100, 30)
        conn.set_terminal_size(120, 40)

        self._drain(peer)
        self._pump(3, until=lambda: channel.resizes)
        self._pump(0.2)

        self.assertEqual([(120, 40)], channel.resizes,
                         "書けるようになったあとに最後の大きさが 1 回だけ届いていない")

    def test_a_resize_waits_during_key_exchange(self):
        conn, channel, peer = self._fake_session()
        self._drain(peer)
        self._pump(0.2)
        channel.transport.clear_to_send.clear()   # 鍵交換中

        conn.set_terminal_size(100, 30)
        self.assertEqual([], channel.resizes, "鍵交換中に window-change を書いた")

        channel.transport.clear_to_send.set()
        self._pump(3, until=lambda: channel.resizes)
        self.assertEqual([(100, 30)], channel.resizes)

    def test_a_writable_transport_hears_the_resize_at_once(self):
        """窓が 0 でも TCP へ書けるなら、今までどおりその場で送る"""
        conn, channel, peer = self._fake_session()
        self._drain(peer)
        self._pump(0.2)
        channel.out_window_size = 0

        conn.set_terminal_size(132, 43)

        self.assertEqual([(132, 43)], channel.resizes)

    def test_resizing_during_a_real_tcp_stall_does_not_block_the_gui(self):
        from core.send_backpressure import socket_writable
        from core.ssh_connection import SSHConnection
        from ui.terminal_widget import TerminalWidget
        server = _SSHServer()
        self.addCleanup(server.close)
        proxy = _StallingProxy(server.port)
        self.addCleanup(proxy.close)
        conn = SSHConnection("127.0.0.1", proxy.port, "u", "p")
        self.addCleanup(conn.dispose)
        errors, closed = [], []
        conn.error_occurred.connect(errors.append)
        conn.disconnected.connect(lambda: closed.append(True))
        self.addCleanup(conn.disconnected.disconnect)
        self.assertTrue(conn.connect(), "前提: localhost の SSH に繋がる")
        self.assertTrue(server.ready.wait(5))
        sock = conn.client.get_transport().sock
        # 送信バッファの大きさを OS に任せない（Windows は自動で広げる）
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        term.key_pressed.connect(conn.send_command)
        term.set_send_backlog(conn.has_pending_sends)
        conn.send_drained.connect(term.resume_send_queue)

        proxy.forward.clear()           # TCP を詰まらせる
        # GUI スレッドが止まっても戻せるよう、別スレッドで 3 秒後に再開する
        resume = threading.Timer(3.0, proxy.forward.set)
        resume.start()
        self.addCleanup(resume.cancel)
        term.send_text(BODY)
        self._pump(2, until=lambda: not socket_writable(sock))
        self.assertFalse(socket_writable(sock), "前提: TCP が詰まっていない")

        started = time.perf_counter()
        conn.set_terminal_size(100, 30)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0,
                        "詰まっている間の set_terminal_size が %.2f 秒止まった"
                        % elapsed)

        expected = BODY.replace("\n", "\r").encode("utf-8")
        self._pump(60, until=lambda: (len(server.received) >= len(expected)
                                      and server.resizes) or errors or closed)
        self._pump(0.3)
        self.assertEqual([], errors)
        self.assertEqual([], closed)
        self.assertEqual(expected, bytes(server.received),
                         "貼り付けが順序どおり全部届いていない")
        self.assertEqual([(100, 30)], server.resizes,
                         "詰まりが解けたあとに大きさが届いていない")


if __name__ == "__main__":
    unittest.main()
