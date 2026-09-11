"""stop() の最中に accept が返った接続を取りこぼさないことを確認する。

待受ループは accept() の後に _client_sockets へ登録し、ハンドラを起こす。
stop() は _stop_event を立て、_client_sockets の一覧を取って閉じる。
accept 復帰から登録までの間に stop() が一覧を取り終えると、その接続は
誰にも閉じられず、停止後にハンドラが起き、SSH バナーを送り、最長
20 秒後に破棄済みかもしれないマネージャへ client_disconnected を emit
する。実測（窓を人為的に広げて再現）: stop() 後に _client_sockets=1、
ハンドラ生存、クライアントへ 'SSH-2.0-paramiko' バナー送信、14 秒後に
client_disconnected。

ここでは accept をゲートで止めて同じ順序を決定的に作る。
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

USER = "netbelt"
PASSWORD = "pw"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _GatedListener:
    """accept() が返った直後（登録前）で止まる待受ソケットの代理。"""

    def __init__(self, real):
        self._real = real
        self.accepted = threading.Event()
        self.release = threading.Event()

    def accept(self):
        result = self._real.accept()
        self.accepted.set()
        self.release.wait(10)
        return result

    def close(self):
        self._real.close()


class SftpStopAcceptRaceTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        from pathlib import Path
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-race-")

    def test_connection_accepted_during_stop_is_closed_not_served(self):
        from core.sftp_server import SFTPServerManager
        m = SFTPServerManager()
        self.addCleanup(m.stop)
        m.STOP_TIMEOUT_SECONDS = 0.5
        port = free_port()
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        deadline = time.time() + 5
        while not m.is_running and time.time() < deadline:
            time.sleep(0.01)
        gate = _GatedListener(m.server_socket)
        m.server_socket = gate
        disconnects = []
        m.client_disconnected.connect(disconnects.append)

        client = socket.socket()
        self.addCleanup(client.close)
        client.settimeout(3)
        client.connect(("127.0.0.1", port))
        self.assertTrue(gate.accepted.wait(5), "前提: accept が返っていない")

        # accept は返ったが登録前。この状態で stop() を完了させる
        m.stop()
        gate.release.set()
        time.sleep(0.5)

        with m._client_lock:
            leftover = len(m._client_sockets)
        self.assertEqual(leftover, 0, "stop() 後に接続が登録されたまま残っている")
        self.assertEqual([t for t in m.client_threads if t.is_alive()], [],
                         "stop() 後にハンドラスレッドが起きている")
        try:
            data = client.recv(64)
        except OSError:
            data = b""
        self.assertFalse(data.startswith(b"SSH-"),
                         "停止後の接続に SSH バナーを返している: %r" % data)
        self.app.processEvents()
        self.assertEqual(disconnects, [],
                         "停止後の接続について client_disconnected が emit された")


    def test_a_registered_connection_is_always_in_client_threads(self):
        """登録済みの接続は必ずハンドラ一覧にも居ること。

        _client_sockets への登録はロックの下だが、ハンドラスレッドの
        start と client_threads への追加はロックの外に残っていた。その
        隙間に stop() が入ると、(a) stop() はそのスレッドを join できず、
        (b) clear() の後に追加されて一覧に 1 本残り、(c) ハンドラは
        stop() 完了後に起きて、破棄済みかもしれないマネージャへ
        client_disconnected を emit する。

        ここでは client_connected を DirectConnection で受けて待受スレッドを
        その隙間に停め、同じ順序を決定的に作る。
        """
        from PyQt6.QtCore import Qt
        from core.sftp_server import SFTPServerManager

        m = SFTPServerManager()
        self.addCleanup(m.stop)
        m.STOP_TIMEOUT_SECONDS = 0.5
        port = free_port()
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        deadline = time.time() + 5
        while not m.is_running and time.time() < deadline:
            time.sleep(0.01)

        stop_returned = threading.Event()
        started_after_stop = []
        original_handle = SFTPServerManager._handle_client

        def _handle(self_, sock, addr):
            if stop_returned.is_set():
                started_after_stop.append(addr[0])
            return original_handle(self_, sock, addr)

        hp = mock.patch.object(SFTPServerManager, "_handle_client", _handle)
        hp.start()
        self.addCleanup(hp.stop)

        parked = threading.Event()
        release = threading.Event()

        def _park(_ip):
            parked.set()
            release.wait(10)

        m.client_connected.connect(_park, Qt.ConnectionType.DirectConnection)

        client = socket.socket()
        self.addCleanup(client.close)
        client.settimeout(3)
        client.connect(("127.0.0.1", port))
        self.assertTrue(parked.wait(5), "前提: 待受スレッドが隙間で止まっていない")

        m.stop()
        stop_returned.set()
        release.set()
        time.sleep(1.0)
        self.app.processEvents()

        self.assertEqual(started_after_stop, [],
                         "stop() 完了後にハンドラが起動している: %r"
                         % started_after_stop)
        self.assertEqual(m.client_threads, [],
                         "stop() の後にハンドラスレッドが一覧へ追加されている: %r"
                         % m.client_threads)


if __name__ == "__main__":
    unittest.main()
