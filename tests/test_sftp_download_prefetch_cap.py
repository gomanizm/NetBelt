# -*- coding: utf-8 -*-
"""ダウンロードの先読み要求数に上限を付けることを検証する。

paramiko の get() は max_concurrent_prefetch_requests を渡さないと、ファイル
全体ぶん（32 KB 単位）の READ 要求を一度に送る。大きいイメージを落とすと、
要求と書き込み前の応答がファイルの大きさに比例してメモリに溜まる。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import paramiko

sys.path.insert(0, "src")


class DownloadPrefetchCapTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, check, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def test_the_download_caps_concurrent_prefetch_requests(self):
        from core.sftp_manager import SFTPManager
        folder = tempfile.mkdtemp(prefix="netbelt-dl-prefetch-")
        self.addCleanup(shutil.rmtree, folder, True)
        m = SFTPManager()
        type(self)._keep.append(m)
        m.is_connected = True
        m.sftp_client = mock.Mock(spec=paramiko.SFTPClient)
        m.sftp_client.get_channel.return_value.closed = False

        def fake_get(remote, local, callback=None, **kwargs):
            with open(local, "wb") as f:
                f.write(b"image")
        m.sftp_client.get.side_effect = fake_get
        done, errors = [], []
        m.transfer_complete.connect(done.append)
        m.error_occurred.connect(errors.append)

        m.download_file("/flash/image.bin", os.path.join(folder, "image.bin"),
                        overwrite=False)

        self.assertTrue(self._pump(lambda: done or errors))
        self.assertEqual(errors, [])
        kwargs = m.sftp_client.get.call_args.kwargs
        cap = kwargs.get("max_concurrent_prefetch_requests")
        self.assertIsInstance(cap, int, "先読みの上限を渡していない")
        self.assertTrue(1 <= cap <= 256, cap)


if __name__ == "__main__":
    unittest.main()
