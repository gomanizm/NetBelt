"""current_path が、画面に一覧が反映されるのと同時に変わることを検証する。

list_directory のワーカースレッドは、一覧を作り終えると
`self.current_path = path` と書いてから file_list_ready を emit していた。
シグナルはキュー経由で GUI スレッドへ届くので、その間（実測 86〜198 µs）
は「マネージャの current_path は新しい場所、画面はまだ古い一覧」に
なる。パネルは操作の開始時に manager.get_current_path() を控える
（_begin）ため、この窓で始めた削除・改名・アップロードは、利用者が
見ていないディレクトリに対して実行される。実測では `/` を表示したまま
`/dirA/top.txt` を消した。

人間には踏めない幅だが、当たると見ていない場所への削除になる。
current_path の更新も GUI スレッドで、一覧の通知と同じ順序で行う。
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Attr:
    def __init__(self, name, is_dir=False):
        self.filename = name
        self.st_mode = 0o040755 if is_dir else 0o100644
        self.st_size = 0 if is_dir else 12
        self.st_mtime = 1700000000


class SftpCurrentPathTimingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        m.is_connected = True
        client = mock.Mock()
        client.listdir_attr.return_value = [_Attr("dirA", is_dir=True),
                                            _Attr("top.txt")]
        client.normalize.side_effect = lambda p: p
        m.sftp_client = client
        return m

    def _wait_for_worker(self, before):
        """list_directory が起こしたスレッドが終わるまで待つ。"""
        deadline = time.time() + 5
        while time.time() < deadline:
            if not [t for t in threading.enumerate() if t not in before]:
                return
            time.sleep(0.005)
        self.fail("一覧取得のスレッドが終わらない")

    def test_the_path_does_not_change_before_the_listing_is_delivered(self):
        """ワーカーが終わっても、GUI が一覧を受け取るまで場所は変わらないこと。"""
        m = self._manager()
        before = set(threading.enumerate())

        m.list_directory("/dirA")
        self._wait_for_worker(before)

        # ここではまだ GUI にシグナルが届いていない
        self.assertEqual(m.get_current_path(), "/",
                         "一覧が画面に届く前に current_path が変わっている")

        self.app.processEvents()
        self.assertEqual(m.get_current_path(), "/dirA",
                         "一覧が届いたのに current_path が変わっていない")

    def test_the_path_is_already_updated_when_the_listing_arrives(self):
        """一覧を受け取る側から見て、そのとき既に場所が更新されていること。

        パネルは file_list_ready のスロットの中で get_current_path() を
        使って表示を組み立てる。通知より後に更新されると今度は逆にずれる。
        """
        m = self._manager()
        seen = []
        m.file_list_ready.connect(lambda _: seen.append(m.get_current_path()))
        before = set(threading.enumerate())

        m.list_directory("/dirA")
        self._wait_for_worker(before)
        self.app.processEvents()

        self.assertEqual(seen, ["/dirA"])

    def test_change_directory_goes_through_the_same_path(self):
        """change_directory も同じ順序で反映されること。"""
        m = self._manager()
        before = set(threading.enumerate())

        m.change_directory("/dirA")
        self._wait_for_worker(before)

        self.assertEqual(m.get_current_path(), "/")
        self.app.processEvents()
        self.assertEqual(m.get_current_path(), "/dirA")


if __name__ == "__main__":
    unittest.main()
