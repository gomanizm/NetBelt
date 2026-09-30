"""SSH で相手が TCP まで受け取りを止めている間に、タブを閉じる・切断の後始末で GUI が止まらないことを検証する。

何が起きていたか（基準 441ea02 で実測、localhost）。構成はクライアント →
中継 → paramiko のシェルサーバ。中継が転送を止めて TCP を詰まらせ、205KB を
貼り付けてから SSHConnection.dispose() を呼ぶと 7.0 秒戻らなかった（中継を
戻すまで）。faulthandler のスタックは ssh_connection.py の dispose →
paramiko channel.close → transport._send_user_message → packet.write_all。
dispose はチャネル → client の順に閉じており、channel.close が相手へ
CHANNEL_CLOSE を書くので、書けるようになるまで待っていた。相手が生きたまま
受け取らなければ無期限で、アプリを落とすしかなくなる。
dispose は MainWindow の後始末（タブを閉じる・切断・エラー・終了）から
GUI スレッドで呼ばれる。しかもタブを閉じる経路などは SSH より先に SFTP の
後始末（SFTPManager.disconnect → SFTPClient.close）を呼ぶ。こちらも SFTP の
チャネルへ CHANNEL_CLOSE を書くので、同じく止まる。

どう直したか。dispose で client（Transport）をチャネルより先に閉じる。
Transport.close は packetizer を閉じてからソケットを閉じ、書き込みを待たない。
そのあとの channel.close は閉じ済みなので何もしない。MainWindow の後始末
（_on_tab_closed・_on_connection_closed・_on_connection_error・closeEvent）は
SSH を SFTP より先に閉じる。Transport が閉じれば SFTP のチャネルも閉じ済みに
なり、SFTPClient.close は何も書かない。相手へは CHANNEL_CLOSE を送らずに
TCP を閉じることになる（機器側はセッションの終了として扱う）。
"""
import json
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
HOLD_SECONDS = 5.0


class _EmptySFTP(paramiko.SFTPServerInterface):
    """空のフォルダだけを見せる SFTP"""

    def list_folder(self, path):
        return []

    def stat(self, path):
        attr = paramiko.SFTPAttributes()
        attr.st_mode = 0o40755
        return attr

    lstat = stat


class _ShellServer(paramiko.ServerInterface):
    def __init__(self):
        self.shell = threading.Event()

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

    def check_channel_window_change_request(self, *args):
        return True


class _SSHServer:
    """シェルと SFTP を持つ localhost の相手（受信ウィンドウ 2MB）"""

    def __init__(self):
        self.received = bytearray()
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
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, _EmptySFTP)
        server = _ShellServer()
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


class _StallMixin:
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

    def _servers(self):
        server = _SSHServer()
        self.addCleanup(server.close)
        proxy = _StallingProxy(server.port)
        self.addCleanup(proxy.close)
        return server, proxy

    def _stall(self, proxy, conn, term):
        """中継を止め、貼り付けで TCP を詰まらせる（HOLD_SECONDS 後に再開）"""
        from core.send_backpressure import socket_writable
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

    def _assert_device_side_closes(self, server, proxy):
        proxy.forward.set()
        self._pump(15, until=lambda: not server.transport.is_active())
        self.assertFalse(server.transport.is_active(),
                         "機器側のセッションが閉じていない")


class SSHDisposeWhileSendBlockedTest(_StallMixin, unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshdispose-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=d)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dispose_during_a_tcp_stall_returns_at_once(self):
        from core.ssh_connection import SSHConnection
        from ui.terminal_widget import TerminalWidget
        server, proxy = self._servers()
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
        self._stall(proxy, conn, term)

        started = time.perf_counter()
        conn.dispose()
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0,
                        "詰まっている間の dispose が %.2f 秒止まった" % elapsed)
        self.assertIsNone(conn.client)
        self.assertIsNone(conn.channel)
        self._assert_device_side_closes(server, proxy)


class MainWindowTabCloseWhileSendBlockedTest(_StallMixin, unittest.TestCase):
    """SFTP を開いたままのタブを、詰まっている最中に閉じる"""

    def setUp(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = Path(tempfile.mkdtemp(prefix="netbelt-sshdispose-win-"))
        self.server, self.proxy = self._servers()
        self.device = {"name": "sw1", "host": "127.0.0.1",
                       "port": self.proxy.port, "protocol": "ssh",
                       "username": "admin", "password": "pw"}
        config_path = d / "config.json"
        config_path.write_text(json.dumps({
            "groups": [{"name": "Default", "devices": [self.device]}],
            "global_macros": []}), encoding="utf-8")
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(config_path))),
                # SFTP の一覧の失敗などで、閉じる相手のいないモーダルを出さない
                mock.patch("ui.sftp_panel.QMessageBox"),
                mock.patch("ui.main_window.QMessageBox")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = MainWindow()
        self.addCleanup(self._close_window)

    def _close_window(self):
        for name in list(self.window.connections):
            self.window._dispose_connection(name)
        with mock.patch("sys.excepthook", lambda *args: None):
            self.window.close()

    def test_closing_the_tab_with_sftp_open_returns_at_once(self):
        window = self.window
        window._on_connect_requested(
            window.config_manager.get_groups()[0]["devices"][0])
        self._pump(20, until=lambda: "sw1" in window.sftp_managers)
        self.assertIn("sw1", window.sftp_managers, "前提: SFTP が開いていない")
        conn = window.connections["sw1"]
        term = window.terminal_widget._terminals["sw1"]
        self._pump(0.3)
        self._stall(self.proxy, conn, term)

        started = time.perf_counter()
        window._on_tab_closed("sw1")
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.5,
                        "詰まっている間にタブを閉じると %.2f 秒止まった" % elapsed)
        self.assertNotIn("sw1", window.connections)
        self.assertNotIn("sw1", window.sftp_managers)
        self._assert_device_side_closes(self.server, self.proxy)


class TeardownOrderTest(unittest.TestCase):
    """MainWindow の後始末は、SSH を SFTP より先に閉じる"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = Path(tempfile.mkdtemp(prefix="netbelt-teardown-order-"))
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(d / "config.json")))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = MainWindow()
        self.addCleanup(self._close_quietly)
        self.order = []
        conn = mock.Mock()
        conn.dispose.side_effect = lambda: self.order.append("ssh")
        conn.disconnect.side_effect = lambda: self.order.append("ssh")
        sftp = mock.Mock()
        sftp.disconnect.side_effect = lambda: self.order.append("sftp")
        self.window.connections["R1"] = conn
        self.window.sftp_managers["R1"] = sftp

    def _close_quietly(self):
        with mock.patch("sys.excepthook", lambda *args: None):
            self.window.close()

    def _assert_ssh_first(self):
        self.assertIn("sftp", self.order, "前提: SFTP が閉じられていない")
        self.assertEqual("ssh", self.order[0],
                         "SSH より先に SFTP を閉じた（SFTP のチャネルへ書く"
                         "CHANNEL_CLOSE で待つ）: %r" % (self.order,))

    def test_closing_the_tab(self):
        self.window._on_tab_closed("R1")
        self._assert_ssh_first()

    def test_the_device_closing_the_session(self):
        self.window._on_connection_closed("R1")
        self._assert_ssh_first()

    def test_a_connection_error(self):
        self.window._on_connection_error("R1", "読み取りエラー: 接続が切れました")
        self._assert_ssh_first()

    def test_closing_the_window(self):
        self.window.close()
        self._assert_ssh_first()


if __name__ == "__main__":
    unittest.main()
