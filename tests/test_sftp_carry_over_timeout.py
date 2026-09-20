"""権限の引き継ぎ（_carry_over_mode）の応答が期限切れになったら、改名へ進まずに接続を畳むことを検証する。

置き換えのアップロードは、全部送ったあと最終名の mode を stat で読み、
一時名へ chmod で当ててから改名する。この stat / chmod は、期限切れも含めて
例外を握りつぶしていた。期限切れのあとは要求と応答がずれたままで、この
接続はもう使えない。それでも改名へ進むので、実測（期限を 3 秒に縮めた
サーバを固めた状態）では stat の期限切れのあと posix_rename でもう一度
期限まで待ち（実際の設定では 2 回で 60 秒）、そのあとも is_connected=True の
まま残った。続けて「新規フォルダ」を実行すると GUI スレッドが期限ぶん止まり、
そこで初めて切断された。

直し方: _carry_over_mode は期限切れだけを握りつぶさずに上へ送る。
upload_thread はそれを受けたら改名へ進まず、転送した内容を一時名に残して
その名前を知らせ、ロックを離してから _fail が接続を畳む（送る前の確認や
転送後の再確認の期限切れと同じ経路）。期限切れでない失敗（mode を当て
られない機器）は、これまでどおり転送の失敗にしない。

改名（posix_rename / rename）そのものの期限切れも、利用者の決定
（2026-09-20）で同じ扱いにそろえた（test_sftp_rename_timeout_disconnect.py）。
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


class SftpCarryOverTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-carry-timeout-")
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
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done, self.disconnected = [], [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        m.disconnected.connect(lambda: self.disconnected.append(True))
        return m

    @staticmethod
    def _existing_mode(mode=0o100600):
        attr = mock.Mock()
        attr.st_mode = mode
        return attr

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _upload(self, m):
        """上書きで送り、一時名（put に渡された名前）を返す"""
        m.upload_file(self.local, FINAL, overwrite=True)
        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.assertEqual(self.done, [], "期限切れなのに完了を通知している")
        return self.client.put.call_args[0][1]

    def _assert_stopped_and_disconnected(self, m, tmp):
        self.client.posix_rename.assert_not_called()
        self.client.rename.assert_not_called()
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [], "転送した内容（一時名）を消している: %s" % removed)
        self.assertTrue(self._wait(lambda: not m.is_connected),
                        "期限切れのあとも接続中のまま残っている")
        self.assertTrue(self._wait(lambda: self.disconnected),
                        "disconnected が出ていない（パネルが切断を知らない）")
        self.assertIsNone(m.sftp_client, "使えないクライアントを掴んだまま")
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("応答しません", self.errors[0],
                      "期限切れで切断した理由が伝わらない: %s" % self.errors)

    def test_a_timed_out_stat_stops_before_the_rename_and_disconnects(self):
        """引き継ぎの stat が期限切れなら、改名へ進まずに接続を畳むこと。"""
        m = self._manager()
        # 1 回目（送る前）は読める。転送後の引き継ぎの stat で機器が黙る
        self.client.stat.side_effect = [self._existing_mode(), TimeoutError()]

        tmp = self._upload(m)

        self._assert_stopped_and_disconnected(m, tmp)

    def test_a_timed_out_chmod_stops_before_the_rename_and_disconnects(self):
        """引き継ぎの chmod が期限切れでも、改名へ進まずに接続を畳むこと。"""
        m = self._manager()
        self.client.stat.return_value = self._existing_mode()
        self.client.chmod.side_effect = TimeoutError()

        tmp = self._upload(m)

        self._assert_stopped_and_disconnected(m, tmp)

    def test_a_refused_chmod_still_completes_the_upload(self):
        """期限切れでない失敗は、これまでどおり握りつぶして置き換えること。"""
        m = self._manager()
        self.client.stat.return_value = self._existing_mode()
        self.client.chmod.side_effect = IOError("Operation unsupported")

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.done or self.errors), "終わらない")
        self.assertEqual(self.errors, [])
        self.client.posix_rename.assert_called_once()
        self.assertTrue(m.is_connected)


if __name__ == "__main__":
    unittest.main()
