"""SFTP の未認証接続が無制限に積み上がらないことを確認する。

_run_server は accept したぶんだけ無条件にソケットを登録し、ハンドラ
スレッドを起こし、その中でさらに paramiko.Transport を作っていた。
待受は 0.0.0.0 なので、LAN 上の任意ホストが認証を通さないまま
スレッドとハンドルを積み上げられる。

同じリポジトリの他サーバには上限がある（TFTP は max_workers=16、
Syslog TCP は max_tcp_connections=64）ので、SFTP だけ揃っていない。

併せて、ソケットの登録がハンドラスレッドの start より前にあるため、
start が失敗するとそのソケットは誰にも閉じられず stop() まで残る。
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


class SftpConnectionCapTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        from pathlib import Path
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-cap-")
        from core.sftp_server import SFTPServerManager
        self.manager = SFTPServerManager()
        self.manager.STOP_TIMEOUT_SECONDS = 1.0
        self.addCleanup(self.manager.stop)

    def _start(self):
        self.port = free_port()
        self.assertTrue(self.manager.start(port=self.port, root_dir=self.root,
                                           username=USER, password=PASSWORD))
        deadline = time.time() + 5
        while not self.manager.is_running and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.manager.is_running, "前提: サーバが起動していない")

    def _client(self):
        c = socket.socket()
        self.addCleanup(c.close)
        c.settimeout(5)
        c.connect(("127.0.0.1", self.port))
        return c

    def _live(self):
        with self.manager._client_lock:
            return len(self.manager._client_sockets)

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _closed_by_server(c, seconds=3.0):
        """サーバ側から閉じられた（EOF が来た）かどうか。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                c.settimeout(0.3)
                return c.recv(1) == b""
            except socket.timeout:
                continue
            except OSError:
                return True
        return False

    def test_there_is_a_default_cap(self):
        self.assertTrue(hasattr(self.manager, "max_client_connections"),
                        "同時接続数の上限が無い")
        self.assertGreater(self.manager.max_client_connections, 0)

    def test_connections_beyond_the_cap_are_closed(self):
        self.manager.max_client_connections = 2
        self._start()
        kept = [self._client() for _ in range(2)]
        self.assertTrue(self._wait(lambda: self._live() >= 2),
                        "前提: 上限ぶんの接続が登録されていない")
        extra = self._client()
        self.assertTrue(self._closed_by_server(extra),
                        "上限を超えた接続が閉じられない")
        self.assertLessEqual(self._live(), 2,
                             "上限を超えた接続が登録されたまま残っている")
        # 既存の接続は巻き込まれていない
        for c in kept:
            self.assertFalse(self._closed_by_server(c, seconds=0.5),
                             "上限内の既存接続まで閉じられている")

    def test_a_slot_freed_by_disconnect_is_reused(self):
        self.manager.max_client_connections = 1
        self._start()
        first = self._client()
        self.assertTrue(self._wait(lambda: self._live() >= 1))
        first.close()
        self.assertTrue(self._wait(lambda: self._live() == 0, seconds=10.0),
                        "切断しても枠が返ってこない")
        second = self._client()
        self.assertTrue(self._wait(lambda: self._live() >= 1),
                        "空いた枠で受け付けない")
        self.assertFalse(self._closed_by_server(second, seconds=0.5),
                         "空いた枠の接続が閉じられている")

    def test_an_unauthenticated_connection_does_not_hold_a_slot_forever(self):
        """バナーも認証も送らない接続が、枠を占有し続けないこと。"""
        self.manager.max_client_connections = 1
        self.manager.banner_timeout_seconds = 1.0
        self.manager.auth_timeout_seconds = 1.0
        self._start()
        idle = self._client()
        self.assertTrue(self._wait(lambda: self._live() >= 1))
        self.assertTrue(self._wait(lambda: self._live() == 0, seconds=10.0),
                        "無通信の接続が枠を占有したまま")

    def test_a_socket_is_not_leaked_when_the_handler_thread_cannot_start(self):
        """ハンドラスレッドを起こせなかった接続を閉じ、登録も残さないこと。"""
        self._start()
        real_start = threading.Thread.start
        failed = threading.Event()

        def _start_failing(thread_self):
            if getattr(thread_self, "_target", None) is not None and \
                    getattr(thread_self._target, "__name__", "") == "_handle_client":
                failed.set()
                raise RuntimeError("can't start new thread")
            return real_start(thread_self)

        sp = mock.patch.object(threading.Thread, "start", _start_failing)
        sp.start()
        try:
            c = self._client()
            self.assertTrue(failed.wait(5), "前提: ハンドラの起動が試みられていない")
            self.assertTrue(self._closed_by_server(c),
                            "起動に失敗した接続が閉じられない")
            self.assertTrue(self._wait(lambda: self._live() == 0),
                            "起動に失敗した接続が登録されたまま残っている")
        finally:
            sp.stop()

        # 待受ループは死んでいない（この後の接続も受け付ける）
        follow = self._client()
        self.assertTrue(self._wait(lambda: self._live() >= 1),
                        "起動失敗の後、待受が止まっている")
        follow.close()


if __name__ == "__main__":
    unittest.main()
