"""遅れて届いた古い一覧が、後から移った先を取り消さないことを検証する。

何が起きていたか（実測）:
  list_directory は 1 件ごとにスレッドを起こし、通信のロック（_sftp_lock）を
  離してから変換・ソート・emit を行う。受け取る _on_listing_done は要求の
  新旧を見ずに current_path = path を書いていた。先に頼んだ /A の変換を
  ロックの外で 0.6 秒遅らせ、その間に /B を頼むと:
    file_list_ready の届いた順: [('/B', ['b.txt']), ('/A', ['a.txt'])]
    最終的な current_path: /A
  /B へ移ったのに、あとから /A へ戻る。逆転の窓は「先の要求がロックを離した
  あとの CPU 処理時間」で、自然な条件では /A が 12 万件のときに再現した
  （2 万件では 0/10、人が押す間隔 500 ms なら 0/10）。表示とパスは
  _update_file_list が同じスロットで揃えるので、見ていない場所への削除・
  アップロードにはならない。症状は「移った先が勝手に戻る」。

どう直したか:
  list_directory に単調増加の要求番号を持たせ（発行時に +1 して控える）、
  _listing_done へ載せる。_on_listing_done は最新の発行番号と一致するとき
  だけ current_path を更新して file_list_ready を出し、古いものは捨てる。
  転送後・削除後の自動更新も同じ経路なので、そのまま最新優先になる。
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

SLOW = 0o100600      # この mode の項目だけ変換を遅らせる（/A の中身）
FAST = 0o100644


class _Attr:
    def __init__(self, name, mode):
        self.filename = name
        self.st_mode = mode
        self.st_size = 1
        self.st_mtime = 1700000000


class SftpStaleListingIgnoredTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        self.app.processEvents()
        return False

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        self.client.normalize.side_effect = lambda p: p
        self.listings = {"/A": [_Attr("a.txt", SLOW)], "/B": [_Attr("b.txt", FAST)]}
        self.client.listdir_attr.side_effect = lambda p: self.listings[p]
        self.seen, self.errors = [], []
        m.file_list_ready.connect(
            lambda fl: self.seen.append((m.get_current_path(),
                                         [f["name"] for f in fl])))
        m.error_occurred.connect(self.errors.append)
        return m

    def _hold_the_conversion_of(self, m, mode, seconds=0.6):
        """ロックを離したあとの変換を、指定の mode の項目だけ引き延ばす"""
        real = m._is_directory
        started = threading.Event()

        def slow(value):
            if value == mode:
                started.set()
                time.sleep(seconds)
            return real(value)

        m._is_directory = slow
        return started

    def test_a_late_listing_does_not_undo_a_newer_move(self):
        """遅れて届いた /A の一覧が、あとから頼んだ /B を取り消さないこと。"""
        m = self._manager()
        started = self._hold_the_conversion_of(m, SLOW)

        m.list_directory("/A")
        self.assertTrue(started.wait(3), "/A の変換が始まらない")
        m.list_directory("/B")          # /A がロックを離したあとで /B へ移る

        self.assertTrue(self._wait(lambda: self.seen), "一覧が届かない")
        time.sleep(1.0)                 # 遅れている /A が届くだけ待つ
        self.app.processEvents()

        self.assertEqual(m.get_current_path(), "/B",
                         "移った先が古い一覧で巻き戻された")
        self.assertEqual(self.seen, [("/B", ["b.txt"])],
                         "古い一覧を捨てていない: %s" % (self.seen,))

    def test_a_single_listing_is_still_delivered(self):
        """1 件だけの一覧は、これまでどおり届いて現在地も変わること。"""
        m = self._manager()

        m.list_directory("/A")

        self.assertTrue(self._wait(lambda: self.seen), "一覧が届かない: %s" % self.errors)
        self.assertEqual(self.seen, [("/A", ["a.txt"])])
        self.assertEqual(m.get_current_path(), "/A")

    def test_listings_that_finish_in_order_are_all_delivered(self):
        """順番どおりに終わる一覧は、どれも捨てられないこと。"""
        m = self._manager()

        m.list_directory("/A")
        self.assertTrue(self._wait(lambda: len(self.seen) == 1))
        m.list_directory("/B")
        self.assertTrue(self._wait(lambda: len(self.seen) == 2),
                        "2 件目が届かない: %s" % (self.seen,))

        self.assertEqual(self.seen, [("/A", ["a.txt"]), ("/B", ["b.txt"])])
        self.assertEqual(m.get_current_path(), "/B")

    def test_a_failed_newer_listing_still_leaves_the_older_one_dropped(self):
        """新しい要求が失敗しても、古い一覧で現在地を書き換えないこと。"""
        m = self._manager()
        started = self._hold_the_conversion_of(m, SLOW)

        m.list_directory("/A")
        self.assertTrue(started.wait(3))
        self.listings["/C"] = None                      # 取得が失敗する
        self.client.listdir_attr.side_effect = IOError("Failure")
        m.list_directory("/C")

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        time.sleep(1.0)
        self.app.processEvents()

        self.assertEqual(self.seen, [], "古い一覧が届いている: %s" % (self.seen,))
        self.assertNotEqual(m.get_current_path(), "/A",
                            "古い一覧が現在地を書き換えた")


if __name__ == "__main__":
    unittest.main()
