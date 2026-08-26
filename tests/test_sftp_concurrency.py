"""SFTP クライアントの同時操作が壊れないことを検証する。

paramiko の SFTPClient は1本のチャンネルを共有する。複数スレッドから
同時に叩くとチャンネル状態が壊れ、実測ではファイルを3つまとめてドロップ
しただけで 3/3 が失敗し、リモートに0バイトのファイルが残った。例外も
上がらず、画面にはエラーすら出なかった。

ドロップは「1操作」で複数スレッドを起こす（sftp_panel の dropEvent が
URL ごとに upload_file を呼ぶ）ので、利用者の普通の操作で踏む。
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, "src")


class SftpConcurrencyTest(unittest.TestCase):
    """実際に SFTP サーバを立て、同時にアップロードして壊れないことを見る。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import unittest.mock
        from pathlib import Path
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        # ホストキーの保存先をユーザーのホームから隔離する
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = unittest.mock.patch(
            "core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)

    def _server(self):
        """ループバックの SFTP サーバを立てて (root, port) を返す。"""
        import socket
        from core.sftp_server import SFTPServerManager

        root = tempfile.mkdtemp(prefix="netbelt-conc-")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        # start(port=0) は self.port に反映されないので、空きポートを先に取る
        server = SFTPServerManager()
        if not server.start(port=port, root_dir=root,
                            username="netbelt", password="netbelt-pass"):
            self.skipTest("SFTP サーバを起動できない")
        self.addCleanup(server.stop)
        deadline = time.time() + 15
        while not server.is_running and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(server.is_running, "SFTP サーバが待ち受けにならない")
        return root, port

    def _manager(self, port):
        """接続済みの SFTPManager を返す。"""
        import paramiko
        from core.sftp_manager import SFTPManager

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

        manager = SFTPManager()
        self.assertTrue(manager.connect(client), "SFTP に接続できない")
        self.addCleanup(manager.disconnect)
        return manager

    def test_dropping_several_files_at_once_uploads_all_of_them(self):
        """複数ファイルの同時アップロードが全部成功すること。

        以前は共有チャンネルが壊れ、リモートに0バイトのファイルが残った。
        """
        from PyQt6.QtWidgets import QApplication

        root, port = self._server()
        manager = self._manager(port)

        local = tempfile.mkdtemp()
        # ファイルごとに中身を変える。同じバイト列だと、混線してもサイズが
        # 合ってしまい「壊れていない」と誤判定する。
        names = ["a.cfg", "b.cfg", "c.cfg"]
        payloads = {n: bytes([ord(n[0])]) * (256 * 1024) for n in names}
        for name in names:
            with open(os.path.join(local, name), "wb") as f:
                f.write(payloads[name])

        errors = []
        manager.error_occurred.connect(errors.append)
        done = []
        manager.transfer_complete.connect(done.append)

        # dropEvent と同じく、1操作で複数スレッドが起きる状況を作る
        for name in names:
            manager.upload_file(os.path.join(local, name), "/" + name)

        deadline = time.time() + 60
        while time.time() < deadline:
            QApplication.processEvents()
            if all(os.path.exists(os.path.join(root, n)) for n in names):
                if all(os.path.getsize(os.path.join(root, n)) == len(payloads[n])
                       for n in names):
                    break
            time.sleep(0.05)

        for name in names:
            path = os.path.join(root, name)
            with self.subTest(name=name):
                self.assertTrue(os.path.exists(path),
                                "%s が届いていない (errors=%s)" % (name, errors))
                with open(path, "rb") as f:
                    got = f.read()
                self.assertEqual(len(got), len(payloads[name]),
                                 "%s が途中で切れている" % name)
                # 中身まで見る。チャンネルが混線すると別ファイルの内容が入る。
                self.assertEqual(got, payloads[name],
                                 "%s の中身が別のファイルと混ざっている" % name)

    def test_a_listing_during_an_upload_does_not_corrupt_it(self):
        """アップロード中に一覧を更新しても転送が壊れないこと。"""
        from PyQt6.QtWidgets import QApplication

        root, port = self._server()
        manager = self._manager(port)

        local = tempfile.mkdtemp()
        payload = b"y" * (512 * 1024)
        src = os.path.join(local, "big.bin")
        with open(src, "wb") as f:
            f.write(payload)

        errors = []
        manager.error_occurred.connect(errors.append)

        manager.upload_file(src, "/big.bin")
        for _ in range(5):
            manager.list_directory("/")
            QApplication.processEvents()

        deadline = time.time() + 60
        target = os.path.join(root, "big.bin")
        while time.time() < deadline:
            QApplication.processEvents()
            if os.path.exists(target) and os.path.getsize(target) == len(payload):
                break
            time.sleep(0.05)

        self.assertTrue(os.path.exists(target), "転送されていない: %s" % errors)
        self.assertEqual(os.path.getsize(target), len(payload),
                         "一覧更新と重なって転送が壊れた")


class SftpGuiLockTest(unittest.TestCase):
    """GUI スレッドから呼ぶ操作は、転送の終わりを待って固まらないこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_a_gui_operation_gives_up_instead_of_freezing(self):
        """転送中の GUI 操作は、待たされずに理由を返すこと。

        待たせると転送が終わるまで画面が固まる。SFTP は数百MBのイメージも
        扱うので、その間ずっと操作不能になるのは受け入れられない。
        """
        from core.sftp_manager import SFTPManager

        manager = SFTPManager()
        manager.is_connected = True
        manager.sftp_client = object()      # 触られないので中身は何でもよい

        errors = []
        manager.error_occurred.connect(errors.append)

        # 転送中を模して、別スレッドがロックを握った状態を作る
        manager._sftp_lock.acquire()
        self.addCleanup(
            lambda: manager._sftp_lock.locked() and manager._sftp_lock.release())

        started = time.time()
        manager.create_directory("/newdir")
        elapsed = time.time() - started

        self.assertLess(elapsed, 3.0,
                        "GUI 操作が長時間ブロックした（%.1f秒）" % elapsed)
        self.assertTrue(errors, "実行できなかったことを伝えていない")
        self.assertIn("転送中", errors[0])


class SftpDisconnectDuringTransferTest(unittest.TestCase):
    """ロック待ちのスレッドがいる状態で切断しても壊れないこと。

    切断は取得前の確認をすり抜ける。待っているあいだに sftp_client が
    None になるので、起きたスレッドがそれを触ると例外や偽のエラーを出す。
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_a_waiting_worker_gives_up_quietly_after_a_disconnect(self):
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        from core.sftp_manager import SFTPManager

        manager = SFTPManager()
        manager.is_connected = True
        manager.sftp_client = mock.Mock()
        errors = []
        manager.error_occurred.connect(errors.append)

        # 転送中を模してロックを握る
        manager._sftp_lock.acquire()
        local = tempfile.mkdtemp()
        src = os.path.join(local, "waiting.cfg")
        with open(src, "wb") as f:
            f.write(b"x" * 16)
        manager.upload_file(src, "/waiting.cfg")   # ロック待ちに入る
        time.sleep(0.3)

        # 待っているあいだに切断（3秒で諦めて戻る）
        manager.disconnect()
        manager._sftp_lock.release()              # 転送側が手を離した

        deadline = time.time() + 5
        while time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.05)

        noisy = [e for e in errors if "NoneType" in e or "AttributeError" in e]
        self.assertEqual(noisy, [], "切断後に例外が漏れている: %s" % noisy)
        self.assertIsNone(manager.sftp_client)


if __name__ == "__main__":
    unittest.main()
