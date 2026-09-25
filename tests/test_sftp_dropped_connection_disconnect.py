"""接続が切れたと分かる失敗でも、SFTP 接続を畳んで再接続を促すことを検証する。

何が起きていたか（実測）:
  _fail は TimeoutError のときだけ接続を畳んでいた。paramiko の
  SSHException('Server connection dropped: ') や素の EOFError は、トランスポート
  が死んだ印なのに接続中のまま残る。mock の機器（overwrite=True、
  posix_rename が SSHException）で送ると is_connected は True のままで
  disconnected も 1 回も出ず、パネルは接続中の表示のまま。以後の一覧・転送は
  すべて同じ失敗を繰り返すだけになる。

利用者の決定（2026-09-20）:
  接続が切れたと分かる失敗も、期限切れと同じく接続を畳んで『接続し直して
  ください』と伝える。置き換わったかが不明な場合は、これまでどおりその旨も
  併せて伝える。

どう直したか:
  _fail に「切断・壊れた応答（EOFError / SFTPError / SSHException）」の枝を足し、
  期限切れと同じく通知してから disconnect する。upload_file は改名の失敗を案内文
  付きの IOError に包み直すので元の型が消える。包むときに、原因が切断・壊れた
  応答だったものだけ _DroppedConnection（IOError の仲間）にして印を残す。
"""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import paramiko

sys.path.insert(0, "src")

FINAL = "/flash/running.cfg"
RECONNECT = "接続し直してください"


class SftpDroppedConnectionDisconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-dropped-")
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

    def _manager(self, existing=True):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        # 畳むと m.sftp_client を手放すので、呼び出しの記録は控えたほうで見る
        self.client = m.sftp_client = mock.Mock()
        if existing:
            attr = mock.Mock()
            attr.st_mode = 0o100600      # 置き換える最終名がある
            self.client.stat.return_value = attr
        else:
            # STAT を実装しない機器。送る直前の確認は「無い」と読む
            self.client.stat.side_effect = IOError("Failure")
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done, self.gone = [], [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        m.disconnected.connect(lambda: self.gone.append(True))
        return m

    def _assert_folded(self, m):
        # 通知は畳む前に出るので、畳み終わるまで少し待つ
        self._wait(lambda: self.gone)
        self.assertTrue(self.errors, "失敗が通知されない")
        self.assertIn(RECONNECT, self.errors[-1],
                      "接続し直すよう伝えていない: %s" % self.errors)
        self.assertFalse(m.is_connected, "接続中のままになっている")
        self.assertEqual(len(self.gone), 1,
                         "切断の通知が 1 回ではない: %d 回" % len(self.gone))

    def test_a_dropped_posix_rename_folds_the_connection(self):
        """切断（SSHException）で落ちた置き換えは、接続も畳むこと。"""
        m = self._manager()
        self.client.posix_rename.side_effect = paramiko.SSHException(
            "Server connection dropped: ")

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors or self.done))
        self._assert_folded(m)
        # 置き換わったか分からないことも、これまでどおり伝わる
        self.assertIn("確かめられませんでした", self.errors[-1], self.errors)
        self.assertIn(self.client.put.call_args[0][1], self.errors[-1],
                      "一時名の在処を知らせていない: %s" % self.errors)

    def test_a_bare_eof_from_an_unconfirmed_rename_folds_the_connection(self):
        """素の EOFError でも同じであること（理由は例外の類名で補う）。"""
        m = self._manager(existing=False)
        self.client.rename.side_effect = EOFError()

        m.upload_file(self.local, FINAL)

        self.assertTrue(self._wait(lambda: self.errors or self.done))
        self._assert_folded(m)
        self.assertIn("EOFError", self.errors[-1], self.errors)

    def test_a_dropped_transfer_folds_the_connection(self):
        """改名まで届かず put が切断で落ちたときも、接続を畳むこと。"""
        m = self._manager(existing=False)
        self.client.put.side_effect = paramiko.SSHException(
            "Server connection dropped: ")

        m.upload_file(self.local, FINAL)

        self.assertTrue(self._wait(lambda: self.errors or self.done))
        self._assert_folded(m)

    def test_a_dropped_listing_folds_the_connection(self):
        """一覧の取得が切断で落ちたときも、接続を畳むこと。"""
        m = self._manager()
        del m.list_directory             # 本物の list_directory を使う
        self.client.listdir_attr.side_effect = paramiko.SFTPError(
            "Garbage packet received")

        m.list_directory("/flash")

        self.assertTrue(self._wait(lambda: self.errors))
        self._assert_folded(m)

    def test_a_dropped_delete_folds_the_connection(self):
        """GUI スレッドから呼ぶ操作（削除）でも同じであること。"""
        m = self._manager()
        self.client.remove.side_effect = EOFError()

        m.delete_item("/flash/old.cfg")

        self.assertTrue(self._wait(lambda: self.errors))
        self._assert_folded(m)

    def test_a_failure_answered_by_the_device_keeps_the_connection(self):
        """機器が答えた失敗（IOError）では、これまでどおり接続を保つこと。"""
        m = self._manager()
        self.client.remove.side_effect = IOError("Permission denied")

        m.delete_item("/flash/old.cfg")

        self.assertTrue(self._wait(lambda: self.errors))
        self.assertIn("Permission denied", self.errors[-1], self.errors)
        self.assertNotIn(RECONNECT, self.errors[-1],
                         "機器が答えた失敗で接続を畳んでいる: %s" % self.errors)
        self.assertTrue(m.is_connected, "機器が答えた失敗で接続を畳んでいる")
        self.assertEqual(self.gone, [])


if __name__ == "__main__":
    unittest.main()
