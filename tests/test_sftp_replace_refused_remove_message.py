"""最終名の remove が断られたあと改名が期限切れになったとき、「最終名は消してある」と言い切らないことを検証する。

posix_rename の無い機器では、rename が「既にある」で断られたときだけ最終名を
remove してから rename し直す。この 2 本目の rename が期限切れになると、
unknown_outcome(e, final_removed=True) で「最終名は置き換えの手順で既に
消してあります。」と書き添える。ところが remove の失敗（IOError）は
握りつぶして 2 本目へ進むので、remove が断られて最終名が残っているときも
同じ文面になっていた。

実測（mock の機器: stat が 0o100600、posix_rename が IOError、1 本目の rename が
IOError、remove が IOError("Permission denied")、2 本目の rename が
TimeoutError）: 文面は「…機器側を確認してください。最終名は置き換えの手順で
既に消してあります。一時名 /flash/.running.cfg.netbelt-part.8476-a2373367 が
残っていれば…」。最終名は消えていないのに、利用者は元の設定が機器から
無くなったと受け取る。

直し方: remove が成功したときだけ final_removed=True にする（断られたときは
最終名について何も言い切らない）。remove が成功した場合の文面は変えない。
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


class SftpReplaceRefusedRemoveMessageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-refused-remove-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _upload(self, remove_side_effect):
        """posix_rename の無い機器で、最終名の remove のあとの rename が期限切れになる"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        attr = mock.Mock()
        attr.st_mode = 0o100600          # 置き換える最終名がある
        self.client.stat.return_value = attr
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        self.client.rename.side_effect = [IOError("Failure"), TimeoutError()]
        self.client.remove.side_effect = remove_side_effect
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)

        m.upload_file(self.local, FINAL, overwrite=True)

        deadline = time.time() + 5
        while time.time() < deadline and not (self.errors or self.done):
            self.app.processEvents()
            time.sleep(0.02)
        self.assertTrue(self.errors, "失敗が通知されない")
        self.assertEqual(self.done, [])
        tmp = self.client.put.call_args[0][1]
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [FINAL], "一時名まで消している: %s" % removed)
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("確かめられませんでした", self.errors[0])

    def test_a_refused_remove_is_not_reported_as_a_removed_final_name(self):
        """remove が断られて最終名が残っているなら、「既に消してあります」と言わないこと。"""
        self._upload(IOError("Permission denied"))

        self.assertNotIn("既に消してあります", self.errors[0],
                         "消えていない最終名を「消した」と言い切っている: %s" % self.errors)

    def test_a_successful_remove_is_still_reported(self):
        """remove が成功したときは、これまでどおり最終名を消したことを伝えること。"""
        self._upload(None)

        self.assertIn("既に消してあります", self.errors[0],
                      "最終名を既に消したことが伝わらない: %s" % self.errors)


if __name__ == "__main__":
    unittest.main()
