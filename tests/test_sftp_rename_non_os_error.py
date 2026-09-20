"""1 本目の改名が切断で落ちたとき、確定した失敗として報告しないことを検証する。

最終名への改名の枝は `except (AttributeError, IOError)` と、代わりの rename
の `except IOError` しか見ていなかった。paramiko は OSError の仲間でない例外
も上げる（EOFError / SFTPError / SSHException("Server connection dropped: ")）
ので、それらは外側の except Exception へ落ちる。そこでは keep_tmp が立って
いないため、後始末が一時名を remove する。改名の要求が機器へ届いたあとに
落ちたのなら置き換わったかどうかは分からないのに、唯一の完全な写しを消した
うえで確定した失敗として報告していた。

実測（mock の機器: stat が 0o100600、overwrite=True、posix_rename が
SSHException("Server connection dropped: ")）: errors=['アップロードエラー:
Server connection dropped: ']、remove の呼び出しに一時名
/flash/.running.cfg.netbelt-part.… が含まれていた。

直し方: どちらの枝も非 OSError を取りこぼさないようにし、置き換わったか
不明な失敗は期限切れの枝と同じ扱い（一時名を残し、状態が不明であることを
伝える）にする。期限切れと違ってチャンネルが使えるとは限らないが、切断の
判断までは広げない（接続の扱いは期限切れの決定のままにする）。
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


class SftpRenameNonOsErrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-nonos-rename-")
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
        self.client = m.sftp_client = mock.Mock()
        if existing:
            attr = mock.Mock()
            attr.st_mode = 0o100600      # 置き換える最終名がある
            self.client.stat.return_value = attr
        else:
            # STAT を実装しない機器。送る直前の確認は「無い」と読む
            self.client.stat.side_effect = IOError("Failure")
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        return m

    def _assert_kept_and_unsure(self):
        self.assertTrue(self.errors, "失敗が通知されない")
        self.assertEqual(self.done, [],
                         "置き換えたか分からないのに完了を通知している: %s" % self.done)
        tmp = self.client.put.call_args[0][1]
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertNotIn(tmp, removed,
                         "転送した唯一の写し（一時名）を消している: %s" % removed)
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("確かめられませんでした", self.errors[0],
                      "置き換わったか不明であることが伝わらない: %s" % self.errors)
        return tmp, removed

    def test_a_dropped_connection_during_posix_rename_keeps_the_copy(self):
        """承認済みの置き換えで posix_rename が切断で落ちたときの扱い。"""
        m = self._manager()
        self.client.posix_rename.side_effect = paramiko.SSHException(
            "Server connection dropped: ")

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        _, removed = self._assert_kept_and_unsure()
        self.assertEqual(removed, [], "最終名まで消しにいっている: %s" % removed)
        self.assertIn("Server connection dropped", self.errors[0],
                      "落ちた理由が伝わらない: %s" % self.errors)
        self.client.rename.assert_not_called()

    def test_a_dropped_connection_during_an_unconfirmed_rename_keeps_the_copy(self):
        """確認を経ていない送信の rename が切断で落ちたときも同じであること。"""
        m = self._manager(existing=False)
        self.client.rename.side_effect = EOFError()

        m.upload_file(self.local, FINAL)

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        _, removed = self._assert_kept_and_unsure()
        self.assertEqual(removed, [], "最終名まで消しにいっている: %s" % removed)
        self.assertIn("EOFError", self.errors[0],
                      "理由の代わりになる例外の類名が出ていない: %s" % self.errors)

    def test_a_dropped_connection_during_the_fallback_rename_keeps_the_copy(self):
        """posix_rename の無い機器で、代わりの rename が切断で落ちたときも同じ。"""
        m = self._manager()
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        self.client.rename.side_effect = paramiko.SFTPError("Garbage packet received")

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        _, removed = self._assert_kept_and_unsure()
        self.assertEqual(removed, [],
                         "置き換わったか分からないのに最終名を消している: %s" % removed)
        self.assertEqual(self.client.rename.call_count, 1,
                         "状態が分からないのに改名をやり直している")

    def test_a_refused_rename_is_still_a_plain_failure(self):
        """機器が答えた失敗（IOError）は、これまでどおりの経路のままであること。"""
        m = self._manager()
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        self.client.rename.side_effect = [IOError("Failure"), None]

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.done or self.errors),
                        "完了もエラーも届かない")
        self.assertEqual(self.errors, [], "承認済みの置き換えが通らなくなっている")
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [FINAL], "復旧手順が変わっている: %s" % removed)


if __name__ == "__main__":
    unittest.main()
