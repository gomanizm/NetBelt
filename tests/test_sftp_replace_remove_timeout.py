"""posix_rename の無い機器への置き換えで、最終名の remove が期限切れになったら止まって接続を畳むことを検証する。

posix_rename の無い機器では、rename が「既にある」で断られたときだけ最終名を
remove してから rename し直す。この remove は `except IOError: pass` で
失敗を握りつぶしており、socket.timeout（TimeoutError）も IOError（OSError）の
仲間なので一緒に握りつぶされていた。

実測（mock の機器: posix_rename が IOError、1 本目の rename が IOError、
remove が TimeoutError、2 本目の rename が TimeoutError）: remove の期限切れの
あとも 2 本目の rename へ進んで、もう一度期限まで待った（実際の設定では
計 60 秒）。そのうえ文面は「最終名は置き換えの手順で既に消してあります。」と
言い切り（消せたかは分かっていない）、is_connected=True のまま残った。
期限切れのあとは要求と応答がずれたままで、この接続はもう使えない。

直し方: remove の `except IOError` の前に `except TimeoutError` を置き、
2 本目の rename へ進まずに抜ける。転送した内容は一時名に残して（keep_tmp）
その名前を知らせ、ロックを離してから _fail が接続を畳む（転送後の再確認や
権限の引き継ぎの期限切れと同じ経路）。文面は「最終名を消せたか確かめられ
ませんでした。転送した内容は一時名 … に残っています」。
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


class SftpReplaceRemoveTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-remove-timeout-")
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

    def test_a_timed_out_remove_stops_before_the_second_rename_and_disconnects(self):
        """最終名の remove が期限切れなら、2 本目の rename へ進まずに接続を畳むこと。"""
        m = self._manager()
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        # 1 本目は「既にある」で断られる。期限切れのあとは要求と応答がずれた
        # ままなので、2 本目に進めばそれも期限切れになる
        self.client.rename.side_effect = [IOError("Failure"), TimeoutError()]
        self.client.remove.side_effect = TimeoutError()

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.assertEqual(self.done, [], "期限切れなのに完了を通知している")
        tmp = self.client.put.call_args[0][1]
        self.assertEqual(self.client.rename.call_count, 1,
                         "remove の期限切れのあとも rename へ進み、もう一度期限まで待っている")
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [FINAL], "転送した内容（一時名）を消している: %s" % removed)
        self.assertTrue(self._wait(lambda: not m.is_connected),
                        "期限切れのあとも接続中のまま残っている")
        self.assertTrue(self._wait(lambda: self.disconnected),
                        "disconnected が出ていない（パネルが切断を知らない）")
        self.assertIsNone(m.sftp_client, "使えないクライアントを掴んだまま")
        self.assertIn("最終名を消せたか確かめられませんでした", self.errors[0])
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("応答しません", self.errors[0],
                      "期限切れで切断した理由が伝わらない: %s" % self.errors)
        self.assertNotIn("既に消してあります", self.errors[0],
                         "消せたか分からない最終名を「消した」と言い切っている")


if __name__ == "__main__":
    unittest.main()
