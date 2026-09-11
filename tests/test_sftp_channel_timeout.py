"""SFTP サブシステムだけ黙った機器で、GUI スレッドの操作が固まらないことを検証する。

mkdir / normalize / chmod などは main_window から GUI スレッドで直接呼ばれる。
共有チャンネルのタイムアウトが None だったので、機器が応答を返さなければ
呼び出し側は無期限に止まり、ターミナルを含むアプリ全体が固まった
（実測: channel.gettimeout()=None、サーバを 4 秒止めると create_directory()
も change_directory() も 4.00 秒そのまま追従）。TCP が切れれば検知できるが、
SFTP サブシステムだけが応答しなくなる機器では抜けられない。

接続直後にチャンネルへ期限を入れ、期限切れはエラー通知で戻す。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpChannelTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from pathlib import Path
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        # ホストキーの保存先をユーザーのホームから隔離する
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)

    def _server(self):
        """ループバックの SFTP サーバを立てて port を返す。"""
        from core.sftp_server import SFTPServerManager

        root = tempfile.mkdtemp(prefix="netbelt-chantimeout-")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        server = SFTPServerManager()
        if not server.start(port=port, root_dir=root,
                            username="netbelt", password="netbelt-pass"):
            self.skipTest("SFTP サーバを起動できない")
        self.addCleanup(server.stop)
        deadline = time.time() + 15
        while not server.is_running and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(server.is_running, "SFTP サーバが待ち受けにならない")
        return port

    def _ssh_client(self, port):
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        deadline = time.time() + 10
        while True:
            try:
                client.connect("127.0.0.1", port=port, username="netbelt",
                               password="netbelt-pass", look_for_keys=False,
                               allow_agent=False, timeout=5)
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.2)
        self.addCleanup(client.close)
        return client

    def test_the_shared_channel_has_a_timeout_after_connect(self):
        """接続直後のチャンネルに期限が入っていること（None のままでない）。"""
        from core.sftp_manager import SFTPManager

        port = self._server()
        manager = SFTPManager()
        self.assertTrue(manager.connect(self._ssh_client(port)), "SFTP に接続できない")
        self.addCleanup(manager.disconnect)

        timeout = manager.sftp_client.get_channel().gettimeout()
        self.assertIsNotNone(timeout, "共有チャンネルに応答待ちの期限が無い")
        self.assertGreater(timeout, 0)

    def test_mkdir_on_a_stalled_server_returns_with_an_error(self):
        """サーバが mkdir に応答しなくても、期限で戻ってエラーを通知すること。

        サーバ側の mkdir を 3 秒止め、クライアント側の期限を 1 秒にする。
        期限が効いていなければ 3 秒そのまま追従する（実測どおり）。
        """
        from core import sftp_server
        from core.sftp_manager import SFTPManager

        stall = 3.0

        def slow_mkdir(handler, path, attr):
            time.sleep(stall)
            return sftp_server.SFTP_OK

        # 期限の属性名は実装側で決める。修正前はこの属性が無く、値も見られない
        # ので create=True で置く（置いても効かないことが、そのまま失敗になる）
        with mock.patch.object(sftp_server.SFTPServerHandler, "mkdir", slow_mkdir), \
             mock.patch.object(SFTPManager, "CHANNEL_TIMEOUT_SECONDS", 1.0,
                               create=True):
            port = self._server()
            manager = SFTPManager()
            self.assertTrue(manager.connect(self._ssh_client(port)), "SFTP に接続できない")
            self.addCleanup(manager.disconnect)
            errors = []
            manager.error_occurred.connect(errors.append)

            started = time.time()
            manager.create_directory("/newdir")
            elapsed = time.time() - started

        self.assertLess(elapsed, stall - 0.5,
                        "サーバの停滞 %.1f 秒をそのまま待っている (%.2f 秒)" % (stall, elapsed))
        self.assertTrue(errors, "期限切れがエラーとして通知されていない")
        self.assertTrue(any("ディレクトリ作成エラー" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
