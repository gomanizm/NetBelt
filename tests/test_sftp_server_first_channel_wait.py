# -*- coding: utf-8 -*-
"""内蔵 SFTP サーバーの最初のチャネル待ちが、認証に掛かった時間を差し引かないことを検証する。

何が起きていたか: _handle_client は鍵交換の直後から transport.accept(timeout=20)
で最初のチャネルを待っていた。paramiko のサーバー側は auth_timeout を見ないので、
パスワードの入力などで認証に 20 秒より長く掛かると、認証の期限
（auth_timeout_seconds = 30 秒）より前に切っていた。逆に、認証しないまま
黙る相手の枠は auth_timeout_seconds ではなく 20 秒で返っていた。

どう直したか: 認証が済むまでは auth_timeout_seconds を期限に待ち、済んでから
最初のチャネルを 20 秒待つ。

確かめ方: 127.0.0.1 のみ。20 秒を待たないよう、サーバー側の accept の待ち時間を
1 秒で打ち切る差し替えを入れ、鍵交換の後 2.5 秒おいてから認証する。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
ACCEPT_CAP_SECONDS = 1.0


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class SftpServerFirstChannelWaitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        import paramiko
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)
        # サーバー側の accept の待ち時間を短く打ち切る（20 秒を 1 秒に）
        real_accept = paramiko.Transport.accept

        def capped_accept(transport, timeout=None):
            if timeout is None or timeout > ACCEPT_CAP_SECONDS:
                timeout = ACCEPT_CAP_SECONDS
            return real_accept(transport, timeout)

        cap = mock.patch.object(paramiko.Transport, "accept", capped_accept)
        cap.start()
        self.addCleanup(cap.stop)
        from core.sftp_server import SFTPServerManager
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-firstchan-")
        self.port = free_tcp_port()
        self.server = SFTPServerManager()
        self.server.host_key = self.host_key
        self.server.STOP_TIMEOUT_SECONDS = 2.0

    def _start(self):
        self.assertTrue(self.server.start(port=self.port, root_dir=self.root,
                                          username=USER, password=PASSWORD))
        self.addCleanup(self.server.stop)

    def _transport_after_kex(self):
        import paramiko
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        transport = paramiko.Transport(sock)
        self.addCleanup(transport.close)
        transport.start_client(timeout=10)
        return transport

    def _closed_within(self, transport, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not transport.is_active():
                return True
            time.sleep(0.05)
        return not transport.is_active()

    def test_a_slow_authentication_still_gets_its_first_channel(self):
        import paramiko
        self._start()
        transport = self._transport_after_kex()
        # 鍵交換の後、最初のチャネル待ち（1 秒に打ち切り）より長く認証に掛かる
        time.sleep(2.5)
        transport.auth_password(USER, PASSWORD)
        sftp = paramiko.SFTPClient.from_transport(transport)
        self.assertEqual(sftp.listdir("."), [])
        sftp.close()

    def test_an_unauthenticated_connection_is_closed_by_the_auth_timeout(self):
        self.server.auth_timeout_seconds = 1.5
        self._start()
        transport = self._transport_after_kex()
        self.assertTrue(self._closed_within(transport, 6.0),
                        "認証しない接続が auth_timeout_seconds を過ぎても閉じられない")

    def test_an_authenticated_connection_without_a_channel_is_still_closed(self):
        self._start()
        transport = self._transport_after_kex()
        transport.auth_password(USER, PASSWORD)
        self.assertTrue(self._closed_within(transport, 6.0),
                        "認証の後チャネルを開かない接続が閉じられない")


if __name__ == "__main__":
    unittest.main()
