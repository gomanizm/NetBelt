"""変更のあとの一覧の自動更新が、まだ一覧の届いていない移動を取り消さないことを検証する。

実測（基準 441ea02）:
  転送の完了・作成・削除・改名・権限変更のあと、SFTPManager は
  list_directory(self.current_path) で一覧を取り直す
  （src/core/sftp_manager.py:951 / 1165 / 1199 / 1227 / 1255）。
  current_path は一覧が GUI スレッドに届いたとき（_on_listing_done）に
  しか変わらないので、/B への移動の一覧が届く前に自動更新が走ると、
  古い /A を頼む。一覧は最後に頼んだ分だけを採る作り（通し番号）なので、
  後から頼んだ /A が先に頼んだ /B を追い越して捨てる。
  アップロードの転送スレッドを transfer_complete の直後（ロックは離れて
  いる）で止め、その間に change_directory('/B') を呼んでから放すと、
  頼んだ一覧の順は ['/B', '/A']、採られた一覧は [['in-A.cfg', 'new.cfg']]、
  current_path='/A'、errors=[] だった。GUI スレッドの削除でも同じになる。
  移動は知らせも無く取り消され、利用者は /B を見ているつもりで /A の
  一覧を操作することになる。

直し方:
  最後に頼んだ一覧の場所を _listing_path に控える（list_directory の中、
  通し番号を進めるのと同じロックの下）。一覧が失敗し、それが最新の要求
  なら控えを None に戻す（失敗した移動先を自動更新が頼み続けないように）。
  自動更新の 5 か所は _refresh_listing() を呼び、控えがあればそこを、
  無ければ current_path を取り直す。利用者の「更新」（list_directory()）は
  変えない。
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


class AutoRefreshKeepsMoveTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.sftp_manager import SFTPManager
        m = self.m = SFTPManager()
        type(self)._keep.append(m)
        m.is_connected = True
        m.current_path = "/A"
        c = m.sftp_client = mock.Mock()
        c.get_channel.return_value.closed = False
        c.normalize.side_effect = lambda p: p
        self.asked = []
        self.asked_lock = threading.Lock()

        def listdir_attr(path):
            with self.asked_lock:
                self.asked.append(path)
            if path == "/C":
                raise PermissionError("Permission denied")
            if path == "/B":
                return [_Attr("in-B.cfg")]
            return [_Attr("in-A.cfg"), _Attr("new.cfg")]
        c.listdir_attr.side_effect = listdir_attr
        self.lists, self.errors = [], []
        m.file_list_ready.connect(
            lambda l: self.lists.append([e["name"] for e in l]))
        m.error_occurred.connect(self.errors.append)

    def _pump(self, check=lambda: False, seconds=1.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _wait_asked(self, count, seconds=5.0):
        """一覧の要求が count 件になるまで待つ（GUI のイベントは回さない）"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            with self.asked_lock:
                if len(self.asked) >= count:
                    return True
            time.sleep(0.01)
        return False

    def test_the_upload_refresh_does_not_cancel_a_pending_move(self):
        """転送完了の自動更新が、一覧の届いていない /B への移動を取り消さないこと。"""
        from PyQt6.QtCore import Qt
        work = tempfile.mkdtemp(prefix="netbelt-refresh-move-")
        self.addCleanup(shutil.rmtree, work, True)
        local = os.path.join(work, "new.cfg")
        with open(local, "w") as f:
            f.write("x")
        upload_done = threading.Event()
        moved = threading.Event()

        def hold_before_refresh(message):
            # 転送スレッドの中（DirectConnection）。ロックは離れている
            upload_done.set()
            moved.wait(5)
        self.m.transfer_complete.connect(hold_before_refresh,
                                         Qt.ConnectionType.DirectConnection)
        self.m.upload_file(local, "/A/new.cfg", overwrite=True)
        self.assertTrue(upload_done.wait(5), "前提: 転送が終わらない")
        self.m.change_directory("/B")     # 利用者の移動。/B の一覧はまだ届かない
        moved.set()                       # 転送スレッドが自動更新へ進む
        self.assertTrue(self._wait_asked(2), "前提: 自動更新が一覧を頼まない")
        self._pump()
        self.assertEqual(self.m.current_path, "/B",
                         "自動更新が移動を取り消した（頼んだ順 %r）" % self.asked)
        self.assertEqual(self.lists[-1], ["in-B.cfg"])
        self.assertEqual(self.errors, [])

    def test_the_delete_refresh_does_not_cancel_a_pending_move(self):
        """GUI スレッドの削除の自動更新も、届いていない移動を取り消さないこと。"""
        self.m.change_directory("/B")      # /B の一覧はまだ届かない
        self.m.delete_item("/A/old.cfg")   # その間に削除（確認なしの設定など）
        self.assertTrue(self._wait_asked(2), "前提: 自動更新が一覧を頼まない")
        self._pump()
        self.assertEqual(self.m.current_path, "/B",
                         "自動更新が移動を取り消した（頼んだ順 %r）" % self.asked)
        self.assertEqual(self.lists[-1], ["in-B.cfg"])
        self.m.sftp_client.remove.assert_called_once_with("/A/old.cfg")

    def test_the_refresh_after_a_failed_move_lists_the_current_directory(self):
        """対照: 移動に失敗したあとの自動更新は、失敗した先ではなく今の場所を取り直すこと。"""
        self.m.change_directory("/C")
        self.assertTrue(self._pump(lambda: self.errors), "前提: /C の一覧が失敗しない")
        self.m.create_directory("/A/newdir")
        self.assertTrue(self._wait_asked(2), "前提: 自動更新が一覧を頼まない")
        self._pump()
        self.assertEqual(self.asked, ["/C", "/A"])
        self.assertEqual(self.m.current_path, "/A")
        self.assertEqual(len(self.errors), 1, self.errors)

    def test_the_refresh_without_a_move_lists_the_current_directory(self):
        """対照: 移動の途中でなければ、これまでどおり今の場所を取り直すこと。"""
        self.m.list_directory()
        self.assertTrue(self._pump(lambda: self.lists), "前提: 一覧が届かない")
        self.m.rename_item("/A/in-A.cfg", "/A/renamed.cfg")
        self.assertTrue(self._wait_asked(2), "前提: 自動更新が一覧を頼まない")
        self._pump()
        self.assertEqual(self.asked, ["/A", "/A"])
        self.assertEqual(self.m.current_path, "/A")


if __name__ == "__main__":
    unittest.main()
