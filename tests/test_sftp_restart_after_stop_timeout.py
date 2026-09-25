"""停止待ちが期限切れになった後の再起動で、旧待受スレッドが新しい状態へ入り込まないこと。

stop() は待受スレッドを STOP_TIMEOUT_SECONDS だけ待ち、抜けなくても
stopped を通知して戻る。start() は旧スレッドを確認せず、同じ _stop_event を
clear() して新しい server_socket・root_dir・資格情報を載せていた。
旧スレッドの accept が後から戻ると、停止判定（共有の _stop_event）を
通過し、旧ポートで受けた接続を新しいルート・新しい資格情報で処理した。
旧スレッドはそのまま新しいソケットで 2 本目の accept ループとして残った。
実測（accept 直後をゲートで止め、STOP_TIMEOUT=0.5 で stop → 別ポート・
別ルート・別資格情報で start → ゲート解除）: 旧スレッド生存、
_client_sockets=1、旧ポートの接続で newuser の認証が通り、新ルートの
ファイルが見えた。

前提の「停止後も待受スレッドが戻らない」状態は人為的なゲートなしでは
作れないが、起きたときに旧スレッドが新しい状態を触る構造そのものを
断つ。直し方: 停止フラグを起動ごとに作り直して待受スレッドへ引数で渡し、
待受ソケットも起動ごとのものを渡す（FTP の _serve(server, stop_event) と
同じ形）。旧スレッドは自分の停止フラグだけを見て、立っていれば受けた
接続を閉じて抜け、後始末でも自分のソケットだけを閉じ、新しい起動の
is_running を倒さない。
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


class SftpRestartAfterStopTimeoutTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.old_root = tempfile.mkdtemp(prefix="netbelt-sftp-old-")
        self.new_root = tempfile.mkdtemp(prefix="netbelt-sftp-new-")
        with open(os.path.join(self.new_root, "new_root_only.txt"), "w") as f:
            f.write("x")

    def _wait(self, cond, timeout=5.0):
        deadline = time.time() + timeout
        while not cond() and time.time() < deadline:
            time.sleep(0.01)
        return cond()

    def test_old_listener_does_not_serve_with_the_restarted_state(self):
        import paramiko
        from core.sftp_server import SFTPServerManager

        m = SFTPServerManager()
        self.addCleanup(m.stop)
        m.STOP_TIMEOUT_SECONDS = 0.5
        old_port, new_port = free_port(), free_port()
        self.assertTrue(m.start(port=old_port, root_dir=self.old_root,
                                username="olduser", password="oldpw"))
        self.assertTrue(self._wait(lambda: m.is_running), "前提: 起動していない")
        old_thread = m.server_thread
        gate = _GatedListener(m.server_socket)
        m.server_socket = gate
        # 実ソケットで始まっている accept(timeout=1) が一巡し、
        # 次の accept からゲートを通るようになるのを待つ
        time.sleep(1.2)

        client = socket.socket()
        self.addCleanup(client.close)
        client.settimeout(1.5)
        client.connect(("127.0.0.1", old_port))
        self.assertTrue(gate.accepted.wait(5), "前提: accept が返っていない")

        # 旧スレッドは accept 直後で止まったまま。停止待ちを期限切れにする
        m.stop()
        self.assertTrue(old_thread.is_alive(), "前提: 停止待ちが期限切れになっていない")

        # 別ポート・別ルート・別資格情報で起動し直してから、旧 accept を戻す
        self.assertTrue(m.start(port=new_port, root_dir=self.new_root,
                                username="newuser", password="newpw"))
        self.assertTrue(self._wait(lambda: m.is_running), "前提: 再起動していない")
        new_thread = m.server_thread
        gate.release.set()

        self._wait(lambda: not old_thread.is_alive(), timeout=3.0)
        self.assertFalse(old_thread.is_alive(),
                         "旧待受スレッドが新しいソケットで待受を続けている")
        with m._client_lock:
            registered = len(m._client_sockets)
        self.assertEqual(registered, 0,
                         "停止済みの旧待受で受けた接続が新しい起動へ登録された")
        try:
            data = client.recv(64)
        except OSError:
            data = b""
        self.assertFalse(data.startswith(b"SSH-"),
                         "停止済みの旧待受で受けた接続を処理している: %r" % data)
        self.assertTrue(m.is_running,
                        "旧スレッドの後始末が新しい起動の is_running を倒した")
        self.assertTrue(new_thread.is_alive(), "新しい待受スレッドが止まっている")

        # 新しい起動はそのまま使える（旧スレッドが新しいソケットを閉じていない）
        transport = paramiko.Transport(("127.0.0.1", new_port))
        self.addCleanup(transport.close)
        transport.start_client(timeout=5)
        transport.auth_password("newuser", "newpw")
        sftp = paramiko.SFTPClient.from_transport(transport)
        self.assertIn("new_root_only.txt", sftp.listdir("/"))
        sftp.close()
        transport.close()

        m.stop()
        self.assertTrue(self._wait(lambda: not new_thread.is_alive(), 3.0))


if __name__ == "__main__":
    unittest.main()
