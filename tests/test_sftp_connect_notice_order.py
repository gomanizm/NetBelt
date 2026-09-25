"""SFTP の接続通知が切断通知に追い越されないことの回帰テスト。

何が起きていたか（実測、基準 f4cad23）: accept ループは _client_lock を
離れてから client_connected を emit していた（src/core/sftp_server.py:661）。
その隙間で相手が切断すると、ハンドラ側の finally が先に client_disconnected
を出せる。accept 側の print を待たせて接続直後に切断したところ、

    emit の順番: ['disconnected', 'connected']
    接続が 0 件のはずの表示: '接続クライアント: 1'

となった。パネルの件数は max(0, n-1) で 0 を下限にするので、順序が入れ替わる
と表示が 1 件ずれたまま、サーバを停止するまで戻らない。
細工なしの接続→即切断 200 回では逆転 0 回（accept 側が常に先着）なので、
実運用で起きるのは accept スレッドが print で詰まったときに限られる。

どう直したか: client_connected.emit を _client_lock を持ったまま
（_notice_shown へ記録するのと同じ区間で）行う。ハンドラ側の切断通知は
同じロックを取ってから出るので、接続の知らせを追い越せない。
パネルの max(0, n-1) は下限として残してある。
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

USER = "netbelt"
PASSWORD = "pw"
# 相手側の通知を待つ上限（秒）。順序が正しければすぐ来る
WAIT_SECONDS = 10.0


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class SftpConnectNoticeOrderTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-order-")

    def _stall_accept_thread_after_the_lock(self, disconnected):
        """accept スレッドを「接続を記録した直後」で待たせる

        core.sftp_server の print を差し替える。基準 f4cad23 では接続の
        emit がこの print の後にあるため、ここで待たせると切断側が先に出る。
        """
        import core.sftp_server as sftp_server

        real_print = print

        def stalling_print(*args, **kwargs):
            message = " ".join(str(a) for a in args)
            real_print(message)
            if message.startswith("[SFTP Server] Client connected from"):
                disconnected.wait(WAIT_SECONDS)

        sftp_server.print = stalling_print
        self.addCleanup(lambda: sftp_server.__dict__.pop("print", None))

    def test_connect_notice_is_emitted_before_disconnect(self):
        from PyQt6.QtCore import Qt
        from core.sftp_server import SFTPServerManager

        order = []
        disconnected = threading.Event()
        self._stall_accept_thread_after_the_lock(disconnected)

        manager = SFTPServerManager()
        self.addCleanup(manager.stop)
        manager.STOP_TIMEOUT_SECONDS = 1.0
        manager.client_connected.connect(
            lambda ip: order.append("connected"),
            Qt.ConnectionType.DirectConnection)

        def on_disconnected(ip):
            order.append("disconnected")
            disconnected.set()

        manager.client_disconnected.connect(on_disconnected,
                                            Qt.ConnectionType.DirectConnection)

        port = free_port()
        self.assertTrue(manager.start(port=port, root_dir=self.root,
                                      username=USER, password=PASSWORD))
        deadline = time.time() + WAIT_SECONDS
        while not manager.is_running and time.time() < deadline:
            time.sleep(0.01)

        client = socket.socket()
        client.settimeout(3)
        client.connect(("127.0.0.1", port))
        client.close()

        deadline = time.time() + WAIT_SECONDS
        while len(order) < 2 and time.time() < deadline:
            time.sleep(0.01)
        manager.stop()

        self.assertEqual(order[:2], ["connected", "disconnected"],
                         "切断の知らせが接続の知らせを追い越しました: %r"
                         % (order,))

    def test_panel_keeps_zero_as_the_floor(self):
        """パネルの下限（負数にしない）はそのまま残す

        順序を core 側で守っても、通知の上限で接続の知らせが省かれる経路は
        残るので、パネルが負数を表示しない備えは外さない。
        """
        from core.config_manager import ConfigManager
        from ui.sftp_server_panel import SFTPServerPanel

        directory = tempfile.mkdtemp(prefix="netbelt-sftp-orderui-")
        panel = SFTPServerPanel(config_manager=ConfigManager(
            config_path=os.path.join(directory, "config.json")))
        self.addCleanup(panel.close)

        panel._on_client_disconnected("192.0.2.10")

        self.assertEqual(panel.connected_clients, 0)
        self.assertEqual(panel.clients_label.text(), "接続クライアント: 0")


if __name__ == "__main__":
    unittest.main()
