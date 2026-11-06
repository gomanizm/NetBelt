# -*- coding: utf-8 -*-
"""Telnet の接続待ちの最中にタブを閉じても、接続スレッドが例外の記録を残さないことを検証する。

何が起きていたか。connect() は socket.create_connection で繋ぐので、待ちの
間（最大 10 秒）は dispose() に閉じるソケットが無い。その間にタブを閉じると
接続オブジェクトは deleteLater で破棄され、あとで待ちが終わった接続スレッドの
error_occurred / output_received の emit が「wrapped C/C++ object ... has been
deleted」の RuntimeError になり、スレッドの traceback がログに残っていた。

相手が応じない状況は、create_connection を差し替えて、テストが合図するまで
待ってから socket.timeout（または接続の成立）を返す形で作る。ネットワークへは
出ない（接続の成立は 127.0.0.1 の受け口へ繋ぐ）。
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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from core import telnet_connection  # noqa: E402


class TabCloseWhileConnectingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-telnet-tabclose-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            self.window = MainWindow()
        self.addCleanup(self._close_window)
        self.thread_errors = []
        patcher = mock.patch.object(
            threading, "excepthook",
            side_effect=lambda args: self.thread_errors.append(
                repr(args.exc_value)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _close_window(self):
        self.window.close()
        self._pump(0.1)

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()

    def _close_tab_while_waiting(self, finish_wait):
        """接続待ちのうちにタブを閉じ、破棄が済んでから待ちを終わらせる"""
        entered = threading.Event()
        release = threading.Event()

        def stalled_create_connection(*args, **kwargs):
            entered.set()
            release.wait(10)
            return finish_wait()

        threads_before = set(threading.enumerate())
        with mock.patch.object(telnet_connection.socket, "create_connection",
                               side_effect=stalled_create_connection):
            self.window._connect_telnet(
                {"name": "dev", "host": "192.0.2.1", "port": 23,
                 "protocol": "telnet", "username": "", "password": ""})
            self.assertTrue(entered.wait(5), "前提: 接続待ちに入った")
            telnet = self.window.connections["dev"]
            self.window._on_tab_closed("dev")
            # deleteLater を済ませる（processEvents だけでは破棄が走らない）
            self._pump(0.1)
            QCoreApplication.sendPostedEvents(
                None, QEvent.Type.DeferredDelete.value)
            self.assertTrue(sip.isdeleted(telnet), "前提: 接続オブジェクトは破棄済み")
            release.set()
            for t in set(threading.enumerate()) - threads_before:
                t.join(5)
        self._pump(0.1)
        self.assertNotIn("dev", self.window.connections)

    def test_timeout_after_tab_close_leaves_no_thread_error(self):
        def timeout():
            raise socket.timeout("timed out")
        self._close_tab_while_waiting(timeout)
        self.assertEqual(self.thread_errors, [])

    def test_late_connection_after_tab_close_leaves_no_thread_error(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(3)
        self.addCleanup(server.close)
        real_create = socket.create_connection
        port = server.getsockname()[1]
        self._close_tab_while_waiting(
            lambda: real_create(("127.0.0.1", port), timeout=3))
        self.assertEqual(self.thread_errors, [])
        # 遅れて成立した接続は閉じられている
        peer, _ = server.accept()
        self.addCleanup(peer.close)
        peer.settimeout(3)
        self.assertEqual(peer.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
