"""順番待ちの間に切断されたダウンロードが、一時ファイルを残さないことを検証する。

download_file は保存先と同じディレクトリに mkstemp で一時ファイル
（*.netbelt-part）を作ってからワーカースレッドを起こす。ワーカーは
_sftp_lock を取ってから接続を確認し直すが、そこで切断済みだと例外を
起こさずに return するため、後始末を書いてある except 節を通らない。
実測: 保存先ディレクトリに 0 バイトの *.netbelt-part が無言で残った。

一時ファイルを消し、何も起きなかった理由を通知すること。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpDownloadQueuedDisconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-dl-queued-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _parts(self):
        return [n for n in os.listdir(self.dir) if n.endswith(".netbelt-part")]

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_a_download_cancelled_while_queued_leaves_no_partial_file(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        errors = []
        m.error_occurred.connect(errors.append)
        local = os.path.join(self.dir, "got.cfg")

        # 先行する転送がロックを握っている状態を作る
        m._sftp_lock.acquire()
        try:
            m.download_file("/flash/running.cfg", local)
            self.assertTrue(self._wait(lambda: self._parts()),
                            "一時ファイルが作られていない（前提が崩れている）")
            # 順番待ちのあいだに切断される
            m.is_connected = False
        finally:
            m._sftp_lock.release()

        self.assertTrue(
            self._wait(lambda: not self._parts()),
            "順番待ちの間に切断され、一時ファイルが残った: %s" % self._parts())
        m.sftp_client.get.assert_not_called()
        self.assertFalse(os.path.exists(local), "保存先に中身の無いファイルができた")
        self.assertTrue(self._wait(lambda: errors),
                        "何も起きなかったことが利用者に伝わらない")


if __name__ == "__main__":
    unittest.main()
