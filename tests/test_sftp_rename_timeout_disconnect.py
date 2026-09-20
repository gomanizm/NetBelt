"""改名そのものが期限切れになったら、接続を畳んで知らせることを検証する。

最終名への改名（posix_rename / rename）が socket.timeout で戻ると、置き
換わったか確かめられない扱い（unknown_outcome）にはなるものの、接続は
is_connected=True のまま残っていた。実測（mock の機器、overwrite=True）:
posix_rename=TimeoutError でも、posix_rename=IOError かつ rename=TimeoutError
でも、posix_rename=IOError・1 本目の rename=IOError・remove 成功・
3 本目の rename=TimeoutError でも、いずれも connected=True で disconnected は
0 回だった。

期限で戻ったあとは要求と応答がずれたままで、このチャンネルはもう使えない。
送る前の確認・転送後の再確認・権限の引き継ぎ・最終名の remove は、どれも
期限切れなら接続を畳んで知らせるのに、改名だけが揃っていなかった。接続中の
まま残ると、次の操作のたびに期限ぶん画面が固まり、しかも再接続すべきだと
分からない。

直し方（利用者の決定 2026-09-20）: 他の期限切れと同じ経路（probe_timed_out /
timed_out_note）へ寄せ、ロックを離してから _fail が接続を畳む。置き換わったか
確かめられないことは、これまでどおり文面で伝える。
"""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

FINAL = "/flash/running.cfg"


class SftpRenameTimeoutDisconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-rename-timeout-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        # 切断すると m.sftp_client は None になるので、呼び出しの記録は控えて見る
        self.client = m.sftp_client = mock.Mock()
        attr = mock.Mock()
        attr.st_mode = 0o100600          # 置き換える最終名がある
        self.client.stat.return_value = attr
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done, self.disconnected = [], [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        m.disconnected.connect(lambda: self.disconnected.append(True))
        return m

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _assert_disconnected_and_told(self, m):
        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertEqual(self.done, [], "期限切れなのに完了を通知している")
        self.assertTrue(self._wait(lambda: not m.is_connected),
                        "期限切れのあとも接続中のまま残っている")
        self.assertTrue(self._wait(lambda: self.disconnected),
                        "disconnected が出ていない（パネルが切断を知らない）")
        self.assertIsNone(m.sftp_client, "使えないクライアントを掴んだまま")
        self.assertEqual(len(self.errors), 1,
                         "同じ失敗を 2 回知らせている: %s" % self.errors)
        tmp = self.client.put.call_args[0][1]
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("確かめられませんでした", self.errors[0],
                      "置き換わったか不明であることが伝わらない: %s" % self.errors)
        self.assertIn("応答しません", self.errors[0],
                      "期限切れで切断した理由が伝わらない: %s" % self.errors)
        self.assertNotIn(tmp, [c[0][0] for c in self.client.remove.call_args_list],
                         "転送した唯一の写し（一時名）を消している")
        return tmp

    def test_a_timed_out_posix_rename_disconnects(self):
        """1 本目（posix_rename）の期限切れで接続を畳むこと。"""
        m = self._manager()
        self.client.posix_rename.side_effect = TimeoutError()

        m.upload_file(self.local, FINAL, overwrite=True)

        self._assert_disconnected_and_told(m)
        self.client.rename.assert_not_called()

    def test_a_timed_out_fallback_rename_disconnects(self):
        """posix_rename の無い機器で、2 本目（rename）の期限切れでも畳むこと。"""
        m = self._manager()
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        self.client.rename.side_effect = TimeoutError()

        m.upload_file(self.local, FINAL, overwrite=True)

        self._assert_disconnected_and_told(m)
        self.assertEqual(self.client.rename.call_count, 1,
                         "期限切れのあとに rename をやり直している")

    def test_a_timed_out_final_rename_disconnects(self):
        """最終名を消したあとの 3 本目の期限切れでも畳み、最終名の行方も伝えること。"""
        m = self._manager()
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        self.client.rename.side_effect = [IOError("Failure"), TimeoutError()]

        m.upload_file(self.local, FINAL, overwrite=True)

        self._assert_disconnected_and_told(m)
        self.assertIn("既に消してあります", self.errors[0],
                      "最終名を消してあることが伝わらない: %s" % self.errors)

    def test_a_refused_rename_does_not_disconnect(self):
        """期限切れでない失敗は、これまでどおり接続を残すこと。"""
        m = self._manager()
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        self.client.rename.side_effect = [IOError("Failure"),
                                          IOError("Permission denied")]

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertTrue(m.is_connected, "使える接続まで畳んでいる")
        self.assertEqual(self.disconnected, [], "disconnected を出している")


if __name__ == "__main__":
    unittest.main()
