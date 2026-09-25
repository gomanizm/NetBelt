"""置き換えの失敗を伝える文面が、理由の無い「: 」で終わらないことを検証する。

最終名を消したあとの rename が失敗したときの枝は、paramiko が上げる
OSError の仲間でない例外（EOFError / SFTPError / SSHException）まで拾うよう
広げられた。ところが文面は `"…に残っています: %s" % (tmp_remote, e)` のまま
で、このリポジトリが 3 か所（_fail、_remote_probe、期限切れの補足）で当てて
いる「str が空なら例外の類名を使う」guard が無い。paramiko は切断した
読み取りで素の EOFError() を上げ、その str は '' なので、まさにこの枝が狙った
例外で理由が消える。

実測（mock の機器: stat が 0o100600、posix_rename が IOError、rename が
[IOError("Failure"), EOFError("")]、remove が成功、overwrite=True）:
errors=['… 機器の一時名 /flash/.running.cfg.netbelt-part.… に残っています: ']
で末尾が「: 」だった。この枝が広がる前は、外側の _fail が guard を当てて
'アップロードエラー: EOFError' と理由を出していた。

直し方: 既存の 3 か所と同じ `str(e) or e.__class__.__name__` にする。
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


class SftpReplaceEmptyReasonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-empty-reason-")
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

    def _replace_failing_with(self, error):
        """posix_rename の無い機器で、最終名を消したあとの rename が error で落ちる"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        attr = mock.Mock()
        attr.st_mode = 0o100600          # 置き換える最終名がある
        self.client.stat.return_value = attr
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        self.client.rename.side_effect = [IOError("Failure"), error]
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.assertTrue(self.errors, "失敗が通知されない")
        return m

    def test_a_reasonless_exception_still_gives_a_reason(self):
        """str が空の例外（素の EOFError）でも、理由の無い「: 」で終わらないこと。"""
        self._replace_failing_with(EOFError())

        self.assertFalse(self.errors[0].rstrip().endswith(":"),
                         "理由が空のまま終わっている: %s" % self.errors)
        self.assertIn("EOFError", self.errors[0],
                      "理由の代わりになる例外の類名が出ていない: %s" % self.errors)

    def test_an_exception_with_a_message_still_shows_it(self):
        """理由が読める例外は、これまでどおりその文言を出すこと。"""
        self._replace_failing_with(IOError("Permission denied"))

        self.assertIn("Permission denied", self.errors[0],
                      "読める理由が消えている: %s" % self.errors)


if __name__ == "__main__":
    unittest.main()
