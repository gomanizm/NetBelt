"""送りかけの一時名を片づける remove が期限切れになったら、接続を畳むことを検証する。

upload_thread の except 節は、一時名の後始末を
`try: … self.sftp_client.remove(tmp_remote) except Exception: pass` で
くるんでいた。socket.timeout（TimeoutError）もここで握りつぶされ、_fail に
渡すのは元の例外（期限切れではない）なので disconnect されない。

実測（mock の機器: stat が 0o100600、put が IOError("Failure")、後始末の
remove が TimeoutError）: errors=['アップロードエラー: Failure'] だけが出て、
is_connected=True・disconnected は出ないまま残った。対照として put 自体が
期限切れなら「…SFTP接続を切断しました」が出て is_connected=False になる。
期限切れのあとは要求と応答がずれたままで、このチャンネルはもう使えない。
接続中のまま残ると、以後の操作のたびに期限ぶん画面が固まる。

直し方: 後始末の `except Exception` の前に `except TimeoutError` を置き、
期限切れの経路（probe_timed_out）へ寄せて、ロックを離してから _fail が
接続を畳む。元の失敗の文面はそのまま先に出す。
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


class SftpCleanupRemoveTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-cleanup-timeout-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _upload(self, remove_side_effect):
        """put が失敗し、送りかけの一時名を片づけにいく"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        # 切断すると m.sftp_client は None になるので、呼び出しの記録は控えて見る
        self.client = m.sftp_client = mock.Mock()
        attr = mock.Mock()
        attr.st_mode = 0o100600          # 置き換える最終名がある
        self.client.stat.return_value = attr
        self.client.put.side_effect = IOError("Failure")
        self.client.remove.side_effect = remove_side_effect
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.disconnected = [], []
        m.error_occurred.connect(self.errors.append)
        m.disconnected.connect(lambda: self.disconnected.append(True))

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertIn("アップロードエラー: Failure", self.errors[0],
                      "元の失敗の文面が変わっている: %s" % self.errors)
        return m

    def test_a_timed_out_cleanup_remove_disconnects(self):
        """後始末の remove が期限切れなら、使えないチャンネルを掴んだまま残らないこと。"""
        m = self._upload(TimeoutError())

        self.assertTrue(self._wait(lambda: not m.is_connected),
                        "期限切れのあとも接続中のまま残っている: %s" % self.errors)
        self.assertTrue(self._wait(lambda: self.disconnected),
                        "disconnected が出ていない（パネルが切断を知らない）")
        self.assertIsNone(m.sftp_client, "使えないクライアントを掴んだまま")
        joined = " / ".join(self.errors)
        self.assertIn("SFTP接続を切断しました", joined,
                      "切断したことが伝わらない: %s" % self.errors)
        tmp = self.client.put.call_args[0][1]
        self.assertIn(tmp, joined,
                      "片づけられなかった一時名を知らせていない: %s" % self.errors)

    def test_a_cleanup_remove_that_works_keeps_the_connection(self):
        """後始末が通ったときは、これまでどおり接続を保つこと。"""
        m = self._upload(None)

        self.assertEqual(self.errors, ["アップロードエラー: Failure"],
                         "余計な通知が増えている: %s" % self.errors)
        self.assertTrue(m.is_connected, "転送の失敗だけで接続を畳んでいる")
        self.assertEqual(self.disconnected, [])
        tmp = self.client.put.call_args[0][1]
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [tmp],
                         "送りかけの一時名を片づけていない: %s" % removed)


if __name__ == "__main__":
    unittest.main()
