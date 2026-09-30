"""保留した端末の大きさが、Transport が書けるようになった時点で機器へ届くことを検証する。

何が起きていたか（c2bb66a で実測、localhost）。1.3.2 の 1 周目で、鍵交換中や
TCP へ書けない間は window-change を送らずに大きさだけを覚え、送信の背圧の
見張り（DrainWatcher）が出す send_drained で最後の大きさを送るようにした。
ところがその見張りは「データを待たずに書けるか」（_send_backlogged）を見て
いて、チャネルの送信ウィンドウが 0 の間も待ち続ける。鍵交換や TCP の詰まりが
解けても、機器が読まずに窓が 0 のままだと send_drained が出ず、window-change
が保留されたままになり、機器側の端末の大きさが古いままだった（折り返しや
ページングが画面と食い違う）。
  - 偽のチャネル・窓 0・鍵交換中に set_terminal_size(100, 30)、鍵交換だけを
    解除して 3 秒待つ: 届いた window-change は []（_size_unsent は True のまま、
    見張りのスレッドは待ち続けていた）
  - 偽のチャネル・窓 0・TCP へ書けない間に変え、TCP だけを戻して 3 秒待つ: []
  - 本物の paramiko（読まない機器・受信ウィンドウ 32KB を使い切って 0）で、
    中継が TCP を止めている間に変え、中継だけを戻して 5 秒待つ: 機器へ届いた
    window-change は []（TCP へは書ける・窓は 0 のまま・エラーも切断も無し）

どう直したか。window-change はチャネルのデータではないので、送信ウィンドウに
縛られない（RFC 4254 5.2 の窓はデータにだけ掛かる。paramiko の resize_pty も
窓を待たない）。保留した大きさには専用の見張りを付け、Transport へいま書くと
待たされるか（鍵交換中・TCP へ書けない。_transport_backlogged）だけを見て、
書けるようになったら GUI スレッドで最後の大きさを 1 回だけ送る。データの
持ち越しの送出（_carry・send_drained）とは独立に動くので、送信の背圧は今までと
同じ（窓が 0 の間、端末に次を渡させない）。GUI スレッドは待たない（見張りは
別のスレッドで状態を読むだけ）。後始末（dispose）ではこの見張りも止める。
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


def _drain(peer):
    """相手側で溜まった分を読み、送る側が書けるようにする"""
    peer.setblocking(False)
    try:
        while True:
            if not peer.recv(65536):
                break
    except (BlockingIOError, OSError):
        pass


def _drain_threads():
    return {t for t in threading.enumerate()
            if t.name == "netbelt-send-drain" and t.is_alive()}


class _FakeTransport:
    def __init__(self, sock):
        self.sock = sock
        self.clear_to_send = threading.Event()
        self.clear_to_send.set()


class _FakeChannel:
    """resize_pty の呼び出しだけを記録するチャネル（偽物）。送信ウィンドウは 0"""

    closed = False

    def __init__(self, transport):
        self.transport = transport
        self.out_window_size = 0     # 機器が読まず、窓が空かない
        self.resizes = []

    def get_transport(self):
        return self.transport

    def resize_pty(self, width, height):
        self.resizes.append((width, height))

    def close(self):
        pass


class ResizeWithAZeroWindowTest(unittest.TestCase):
    """偽のチャネルで、窓が 0 のまま Transport だけが戻る場合"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _fake_session(self):
        """connect() と同じ見張りを持つ SSHConnection に、偽のチャネルを差す"""
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
        # 見張りを残さない（dispose は見張りをすべて止める）
        self.addCleanup(conn.dispose)
        return conn, channel, b

    def test_the_size_goes_out_when_only_the_key_exchange_ends(self):
        conn, channel, peer = self._fake_session()
        _drain(peer)
        self._pump(0.2)
        channel.transport.clear_to_send.clear()   # 鍵交換中

        conn.set_terminal_size(100, 30)
        self.assertEqual([], channel.resizes, "鍵交換中に window-change を書いた")

        channel.transport.clear_to_send.set()     # 鍵交換だけが終わる（窓は 0 のまま）
        self._pump(3, until=lambda: channel.resizes)
        self._pump(0.2)
        self.assertEqual([(100, 30)], channel.resizes,
                         "鍵交換が終わっても、窓が 0 の間 window-change が保留された")

    def test_the_last_size_goes_out_once_when_only_the_tcp_recovers(self):
        conn, channel, peer = self._fake_session()

        conn.set_terminal_size(100, 30)
        conn.set_terminal_size(120, 40)
        self.assertEqual([], channel.resizes, "TCP へ書けない間に window-change を書いた")

        _drain(peer)                              # TCP だけが戻る（窓は 0 のまま）
        self._pump(3, until=lambda: channel.resizes)
        self._pump(0.2)
        self.assertEqual([(120, 40)], channel.resizes,
                         "TCP が戻っても、窓が 0 の間 window-change が保留された"
                         "（または最後の大きさが 1 回だけ届いていない）")

    def test_the_data_stays_held_back_while_the_window_is_zero(self):
        """大きさを送ったあとも、窓が 0 の間は端末に次の区切りを渡させない"""
        conn, channel, peer = self._fake_session()
        drained = []
        conn.send_drained.connect(lambda: drained.append(True))
        channel.transport.clear_to_send.clear()
        conn.set_terminal_size(100, 30)

        _drain(peer)
        channel.transport.clear_to_send.set()
        self._pump(3, until=lambda: channel.resizes)
        self._pump(0.2)

        self.assertEqual([(100, 30)], channel.resizes)
        self.assertTrue(conn.has_pending_sends(),
                        "窓が 0 なのに、端末に次の区切りを渡させる")
        self.assertEqual([], drained,
                         "窓が 0 なのに、書けるようになった知らせ（send_drained）が出た")

    def test_dispose_stops_waiting_for_the_size(self):
        conn, channel, _ = self._fake_session()
        before = _drain_threads()
        channel.transport.clear_to_send.clear()
        conn.set_terminal_size(100, 30)
        started = _drain_threads() - before
        self.assertTrue(started, "前提: 大きさを待つ見張りが動いていない")

        conn.dispose()

        self.assertEqual(set(), {t for t in started if t.is_alive()},
                         "dispose のあとも見張りが残っている")
        channel.transport.clear_to_send.set()
        self._pump(0.3)
        self.assertEqual([], channel.resizes, "後始末のあとに window-change を書いた")


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


class _NonReadingSSHServer:
    """チャネルを読まない localhost のシェル（受信ウィンドウ 32KB を空けない）"""

    WINDOW = 32768     # paramiko が受け付ける最小の窓

    def __init__(self):
        self.resizes = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.ready = threading.Event()
        self.transport = None
        self.channel = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            accepted, _ = self.sock.accept()
        except OSError:
            return
        t = paramiko.Transport(accepted)
        self.transport = t
        t.default_window_size = self.WINDOW
        t.add_server_key(paramiko.ECDSAKey.generate())
        server = _ShellServer(self.resizes)
        try:
            t.start_server(server=server)
        except Exception:
            return
        ch = t.accept(10)
        if ch is None or not server.shell.wait(10):
            return
        self.channel = ch      # 読まない（窓を補充しない）
        self.ready.set()

    def close(self):
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


class ResizeToADeviceThatDoesNotReadTest(unittest.TestCase):
    """本物の paramiko で、機器が読まない（窓 0）まま TCP の詰まりだけが解ける場合"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshresize0-"))
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

    def test_the_size_reaches_the_device_once_the_tcp_stall_ends(self):
        from core.send_backpressure import socket_writable
        from core.ssh_connection import SSHConnection
        server = _NonReadingSSHServer()
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
        transport = conn.client.get_transport()
        sock = transport.sock
        # 送信バッファの大きさを OS に任せない（Windows は自動で広げる）
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

        # 機器が読まないので、窓を使い切ると 0 のまま補充されない
        conn.send_command("x" * (_NonReadingSSHServer.WINDOW + 4096))
        self._pump(3, until=lambda: conn.channel.out_window_size == 0)
        self.assertEqual(0, conn.channel.out_window_size, "前提: 窓が 0 になっていない")

        # TCP を止め、窓と関係のない SSH_MSG_IGNORE で送信バッファを埋める
        # （書き込みで止まるのはこの別スレッドだけ。中継を戻せば抜ける）。
        # 詰まったら埋めるのをやめる（戻ったあとも埋め続けると、戻った TCP を
        # また詰まらせる）
        proxy.forward.clear()
        stuffed = threading.Event()

        def stuff():
            try:
                while not stuffed.is_set() and socket_writable(sock):
                    transport.send_ignore(4096)
            except Exception:
                pass
        stuffer = threading.Thread(target=stuff, daemon=True)
        stuffer.start()
        self.addCleanup(stuffer.join, 5)
        self.addCleanup(proxy.forward.set)
        self.addCleanup(stuffed.set)
        self._pump(5, until=lambda: not socket_writable(sock))
        stuffed.set()
        self.assertFalse(socket_writable(sock), "前提: TCP が詰まっていない")

        # GUI スレッドが止まっても戻せるよう、別スレッドで 3 秒後に再開する
        resume = threading.Timer(3.0, proxy.forward.set)
        resume.start()
        self.addCleanup(resume.cancel)
        started = time.perf_counter()
        conn.set_terminal_size(100, 30)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0,
                        "詰まっている間の set_terminal_size が %.2f 秒止まった"
                        % elapsed)

        resume.cancel()
        proxy.forward.set()          # TCP だけが戻る（機器は読まないまま）
        self._pump(10, until=lambda: server.resizes or errors or closed)
        self._pump(0.3)
        self.assertEqual(0, conn.channel.out_window_size,
                         "前提: 窓が 0 のままではない（機器が読んだ）")
        self.assertEqual([], errors)
        self.assertEqual([], closed)
        self.assertEqual([(100, 30)], server.resizes,
                         "TCP の詰まりが解けても、窓が 0 の間 window-change が届かない")


if __name__ == "__main__":
    unittest.main()
