"""古い一覧の失敗が、あとから頼んだ移動の行き先の控えを消さないことを検証する
（テストの穴を埋める）。

実測（b2858c4。本体は正しく、テストが守っていなかった）:
  一覧が失敗したときに控え（_listing_path）を外すのは、その要求が最新のとき
  だけだった（src/core/sftp_manager.py:488 の if seq == self._listing_seq:）。
  この確かめを if True: にしても、SFTP クライアントに関わるテスト 83 ファイル
  （517 件）はすべて通った。外すと、読めない /C の一覧が、あとから頼んだ /B への
  移動より後に失敗したときに /B の控えが消え、続く自動更新が今の /A を頼む。
  /B の一覧は届いても捨てられ、頼んだ順は ['/C', '/B', '/A']、current_path は
  '/A'、エラーは /C の 1 件だけで、移動が知らせも無く取り消される。
  その後の直し（自動更新が失敗した移動を追わない）で、確かめは「控えを置いた
  要求か」（_forget_failed_destination の if seq == self._listing_path_seq:）に
  変わった。守る振る舞いは同じで、こちらを if True: にしてもこのテストは落ちる。

守り方:
  その並びを作り、/C の失敗のあとの自動更新が /B を取り直し、移動が保たれる
  ことを確かめる。
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


class OldListingFailureKeepsMoveTest(unittest.TestCase):
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

    def test_an_older_failed_listing_does_not_cancel_a_later_move(self):
        """読めない /C の一覧が /B への移動より後に失敗しても、移動が取り消されないこと。"""
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        type(self)._keep.append(m)
        m.is_connected = True
        m.current_path = "/A"
        c = m.sftp_client = mock.Mock()
        c.get_channel.return_value.closed = False
        c.normalize.side_effect = lambda p: p
        asked = []
        asked_lock = threading.Lock()
        c_gate = threading.Event()
        b_gate = threading.Event()
        self.addCleanup(c_gate.set)
        self.addCleanup(b_gate.set)

        def listdir_attr(path):
            with asked_lock:
                asked.append(path)
            # 機器の答えを待つあいだ、通信のロックを持ったまま止まる
            if path == "/C":
                c_gate.wait(5)
                raise PermissionError("Permission denied")
            if path == "/B":
                b_gate.wait(5)
                return [_Attr("in-B.cfg")]
            return [_Attr("in-A.cfg")]
        c.listdir_attr.side_effect = listdir_attr
        lists, errors = [], []
        m.file_list_ready.connect(lambda l: lists.append([e["name"] for e in l]))
        m.error_occurred.connect(errors.append)

        def wait_asked(path):
            deadline = time.time() + 5
            while time.time() < deadline:
                with asked_lock:
                    if path in asked:
                        return True
                time.sleep(0.01)
            return False

        m.change_directory("/C")           # 利用者の移動。/C の一覧はロックの中で止まる
        self.assertTrue(wait_asked("/C"), "前提: /C の一覧が始まらない")
        # 続けて /B へ。change_directory は通信のロックを 0.5 秒しか待たないので、
        # それが normalize のあとに呼ぶのと同じ list_directory を直に呼ぶ
        m.list_directory("/B")
        c_gate.set()                       # /C が失敗する（もう最新の要求ではない）
        self.assertTrue(self._pump(lambda: errors, seconds=5.0),
                        "前提: /C の失敗が知らされない")
        self.assertTrue(wait_asked("/B"), "前提: /B の一覧が始まらない")
        # /B の一覧が届く前の自動更新（転送スレッドが完了のあとに呼ぶのと同じ）
        m._refresh_listing()
        b_gate.set()
        self._pump(lambda: lists, seconds=5.0)
        self._pump(seconds=0.5)            # 遅れて届くものが無いこと
        self.assertEqual(m.current_path, "/B",
                         "古い失敗が移動を取り消した（頼んだ順 %r）" % asked)
        self.assertEqual(asked, ["/C", "/B", "/B"])
        self.assertEqual(lists[-1], ["in-B.cfg"])
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("Permission denied", errors[0])


if __name__ == "__main__":
    unittest.main()
