"""SFTP サブシステムだけ黙った機器で、GUI スレッドの操作が固まらないことを検証する。

mkdir / normalize / chmod などは main_window から GUI スレッドで直接呼ばれる。
共有チャンネルのタイムアウトが None だったので、機器が応答を返さなければ
呼び出し側は無期限に止まり、ターミナルを含むアプリ全体が固まった
（実測: channel.gettimeout()=None、サーバを 4 秒止めると create_directory()
も change_directory() も 4.00 秒そのまま追従）。TCP が切れれば検知できるが、
SFTP サブシステムだけが応答しなくなる機器では抜けられない。

接続直後にチャンネルへ期限を入れ、期限切れはエラー通知で戻す。
"""
import io
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


    def test_a_timed_out_operation_says_why_and_closes_the_session(self):
        """期限切れのあと、使えないセッションが「接続中」で残らないこと。

        期限で戻っても要求と応答はずれたままなので、同じチャンネルの以後の
        操作は失敗し続ける。それでも is_connected が True だと、利用者は操作の
        たびに期限ぶん固まったうえ、理由の書かれていないエラー（socket.timeout
        は str が空）を見続けることになり、再接続すべきだと分からない。
        """
        from core import sftp_server
        from core.sftp_manager import SFTPManager

        def slow_mkdir(handler, path, attr):
            time.sleep(3.0)
            return sftp_server.SFTP_OK

        with mock.patch.object(sftp_server.SFTPServerHandler, "mkdir", slow_mkdir),              mock.patch.object(SFTPManager, "CHANNEL_TIMEOUT_SECONDS", 1.0,
                               create=True):
            port = self._server()
            manager = SFTPManager()
            self.assertTrue(manager.connect(self._ssh_client(port)), "SFTP に接続できない")
            self.addCleanup(manager.disconnect)
            errors = []
            manager.error_occurred.connect(errors.append)

            manager.create_directory("/newdir")

            # 理由の書かれたメッセージであること（空の str(e) を貼っただけでない）
            self.assertTrue(any("応答しません" in e for e in errors), errors)
            self.assertFalse(
                manager.is_connected,
                "使用不能になったセッションが接続中のまま残っている")

            # 以後の操作は、期限ぶん固まらずに未接続として即座に戻る
            errors.clear()
            started = time.time()
            manager.create_directory("/newdir2")
            elapsed = time.time() - started

        self.assertLess(elapsed, 0.5,
                        "切断済みのはずが、また期限まで待っている (%.2f 秒)" % elapsed)
        self.assertEqual(errors, ["SFTP接続がありません"], errors)


class SftpTransferTimeoutTest(unittest.TestCase):
    """転送が期限切れで終わったときも、理由を出して接続を畳むこと。

    put / get の途中で機器が黙ると、チャンネルの期限で socket.timeout に
    なる。GUI スレッドの操作と同じで、以後この接続は使えないうえ、
    socket.timeout は str が空なので「アップロードエラー: 」としか出ない。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-xfer-timeout-")

    def _manager(self):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.sftp_client.stat.side_effect = FileNotFoundError("gone")  # 送り先は空
        m.list_directory = mock.Mock()
        self.errors = []
        m.error_occurred.connect(self.errors.append)
        return m

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_an_upload_that_times_out_says_why_and_closes_the_session(self):
        m = self._manager()
        local = os.path.join(self.dir, "running.cfg")
        with io.open(local, "w", encoding="utf-8") as f:
            f.write("hostname R1")
        m.sftp_client.put.side_effect = TimeoutError()

        m.upload_file(local, "/flash/running.cfg", overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertTrue(any("応答しません" in e for e in self.errors), self.errors)
        self.assertTrue(self._wait(lambda: not m.is_connected),
                        "使用不能になったセッションが接続中のまま残っている")

    def test_a_download_that_times_out_says_why_and_closes_the_session(self):
        m = self._manager()
        m.sftp_client.get.side_effect = TimeoutError()

        m.download_file("/flash/running.cfg", os.path.join(self.dir, "got.cfg"))

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertTrue(any("応答しません" in e for e in self.errors), self.errors)
        self.assertTrue(self._wait(lambda: not m.is_connected),
                        "使用不能になったセッションが接続中のまま残っている")


if __name__ == "__main__":
    unittest.main()
