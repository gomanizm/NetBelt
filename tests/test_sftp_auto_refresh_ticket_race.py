"""転送スレッドの自動更新が、場所を読んでから番号を取るまでの間に入った利用者の移動を
取り消さないことを検証する。

実測（e34c1ac）:
  SFTPManager._refresh_listing は、最後に頼んだ場所（_listing_path）を読む
  ロック区間と、list_directory が通し番号を進めるロック区間が別だった
  （src/core/sftp_manager.py:478-480 と 374-377）。転送スレッドの自動更新が
  '/B' を読んだところで、GUI スレッドの利用者が /D へ移動する
  （change_directory -> list_directory('/D')）と、読んだ古い '/B' が後から番号を
  取って移動を追い越す。頼んだ順は [..., '/D', '/B']、current_path は '/B' で、
  /D への移動が知らせも無く取り消された。窓は 2 つのロック区間の間の数行だけ。

直し方:
  自動更新の場所は、番号を進めるのと同じロック区間の中で選ぶ
  （list_directory(refresh=True)）。移動がその前なら自動更新は /D を頼み、
  後なら /D のほうが新しい番号を持つ。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Attr:
    def __init__(self, name):
        self.filename = name
        self.st_mode = 0o100644
        self.st_size = 1
        self.st_mtime = 0


class _PausingLock:
    """_listing_seq_lock の代わり。転送スレッドが最初に手を離した直後で止める"""

    def __init__(self):
        self._inner = threading.Lock()
        self.worker = None
        self.paused = threading.Event()
        self.resume = threading.Event()

    def acquire(self, *args, **kwargs):
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        self._inner.release()

    def __enter__(self):
        self._inner.acquire()
        return self

    def __exit__(self, *exc):
        self._inner.release()
        if (threading.current_thread() is self.worker
                and not self.paused.is_set()):
            self.paused.set()
            self.resume.wait(5)
        return False


class AutoRefreshTicketRaceTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, check=lambda: False, seconds=1.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def test_a_move_between_reading_and_ticketing_is_not_cancelled(self):
        """自動更新が場所を選んだ直後に入った /D への移動が、取り消されないこと。"""
        from PyQt6.QtCore import Qt
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        type(self)._keep.append(m)
        m.is_connected = True
        m.current_path = "/B"
        c = m.sftp_client = mock.Mock()
        c.get_channel.return_value.closed = False
        c.normalize.side_effect = lambda p: p
        asked = []
        asked_lock = threading.Lock()

        def listdir_attr(path):
            with asked_lock:
                asked.append(path)
            if path == "/D":
                return [_Attr("in-D.cfg")]
            return [_Attr("in-B.cfg"), _Attr("new.cfg")]
        c.listdir_attr.side_effect = listdir_attr
        lists, errors = [], []
        m.file_list_ready.connect(lambda l: lists.append([e["name"] for e in l]))
        m.error_occurred.connect(errors.append)
        m.list_directory()
        self.assertTrue(self._pump(lambda: lists), "前提: /B の一覧が届かない")

        lock = m._listing_seq_lock = _PausingLock()
        m.transfer_complete.connect(
            lambda message: setattr(lock, "worker", threading.current_thread()),
            Qt.ConnectionType.DirectConnection)
        work = tempfile.mkdtemp(prefix="netbelt-refresh-race-")
        self.addCleanup(shutil.rmtree, work, True)
        local = os.path.join(work, "new.cfg")
        with open(local, "w") as f:
            f.write("x")
        m.upload_file(local, "/B/new.cfg", overwrite=True)
        try:
            self.assertTrue(lock.paused.wait(5), "前提: 自動更新が一覧を頼まない")
            m.change_directory("/D")        # 利用者の移動（GUI スレッド）
        finally:
            lock.resume.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            with asked_lock:
                if len(asked) >= 3:
                    break
            time.sleep(0.01)
        self._pump(seconds=1.0)
        self.assertEqual(m.current_path, "/D",
                         "自動更新が移動を取り消した（頼んだ順 %r）" % asked)
        self.assertEqual(lists[-1], ["in-D.cfg"])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
