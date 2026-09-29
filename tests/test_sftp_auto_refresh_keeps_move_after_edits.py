"""新規フォルダ・名前変更・パーミッション変更のあとの自動更新が、まだ一覧の
届いていない移動を取り消さないことを検証する（テストの穴を埋める）。

実測（b2858c4。本体は正しく、テストが守っていなかった）:
  変更のあとの自動更新は 5 か所とも _refresh_listing() を呼ぶ
  （src/core/sftp_manager.py の upload 1047・mkdir 1268・delete 1302・
  rename 1330・chmod 1358）。ところがテストが確かめていたのはアップロードと
  削除の 2 経路だけで、mkdir・rename・chmod の 3 か所を 441ea02 の
  list_directory(self.current_path) に戻しても、SFTP クライアントに関わる
  テスト 83 ファイル（517 件）はすべて通った。戻すと、/B への移動の一覧が
  届く前に操作したとき、頼んだ順は ['/B', '/A']、current_path は '/A'、
  最後の一覧は /A のものになり、移動が知らせも無く取り消される（利用者は
  /B を見ているつもりで /A の一覧を操作する）。この 3 経路は名前や
  パーミッションの入力ダイアログを挟むので、遅い機器で大きなディレクトリの
  一覧を待つ間に OK が押されうる。

守り方:
  3 つの操作それぞれで、/B への移動の一覧が GUI に届く前に操作し、
  自動更新が /B を取り直して移動が保たれることを確かめる（削除の
  test_the_delete_refresh_does_not_cancel_a_pending_move と同じ形）。
"""
import sys
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


class AutoRefreshKeepsMoveAfterEditsTest(unittest.TestCase):
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
            if path == "/B":
                return [_Attr("in-B.cfg")]
            return [_Attr("in-A.cfg"), _Attr("new.cfg")]
        c.listdir_attr.side_effect = listdir_attr
        self.lists, self.errors = [], []
        m.file_list_ready.connect(
            lambda l: self.lists.append([e["name"] for e in l]))
        m.error_occurred.connect(self.errors.append)

    def _wait_asked(self, count, seconds=5.0):
        """一覧の要求が count 件になるまで待つ（GUI のイベントは回さない）"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            with self.asked_lock:
                if len(self.asked) >= count:
                    return True
            time.sleep(0.01)
        return False

    def _pump(self, seconds=1.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

    def _assert_move_kept(self):
        self.assertTrue(self._wait_asked(2), "前提: 自動更新が一覧を頼まない")
        self._pump()
        self.assertEqual(self.m.current_path, "/B",
                         "自動更新が移動を取り消した（頼んだ順 %r）" % self.asked)
        self.assertEqual(self.asked, ["/B", "/B"])
        self.assertEqual(self.lists[-1], ["in-B.cfg"])
        self.assertEqual(self.errors, [])

    def test_the_mkdir_refresh_does_not_cancel_a_pending_move(self):
        """新規フォルダの自動更新が、一覧の届いていない /B への移動を取り消さないこと。"""
        self.m.change_directory("/B")          # /B の一覧はまだ届かない
        self.m.create_directory("/A/newdir")   # その間に名前の入力で OK
        self.m.sftp_client.mkdir.assert_called_once_with("/A/newdir")
        self._assert_move_kept()

    def test_the_rename_refresh_does_not_cancel_a_pending_move(self):
        """名前変更の自動更新が、一覧の届いていない /B への移動を取り消さないこと。"""
        self.m.change_directory("/B")
        self.m.rename_item("/A/in-A.cfg", "/A/renamed.cfg")
        self.m.sftp_client.rename.assert_called_once_with(
            "/A/in-A.cfg", "/A/renamed.cfg")
        self._assert_move_kept()

    def test_the_chmod_refresh_does_not_cancel_a_pending_move(self):
        """パーミッション変更の自動更新が、一覧の届いていない /B への移動を取り消さないこと。"""
        self.m.change_directory("/B")
        self.m.change_permissions("/A/in-A.cfg", 0o600)
        self.m.sftp_client.chmod.assert_called_once_with("/A/in-A.cfg", 0o600)
        self._assert_move_kept()


if __name__ == "__main__":
    unittest.main()
