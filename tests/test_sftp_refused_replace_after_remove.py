"""最終名を消したあとの置き換えが IOError で断られたとき、消したことを伝えるか検証する。

posix_rename の無い機器では、1 本目の rename が「既にある」で断られたときだけ
最終名を remove してから rename し直す。この 2 本目の rename が期限切れに
なる枝は unknown_outcome(e, final_removed=...) で「最終名は置き換えの手順で
既に消してあります。」を書き添えるが、隣の「IOError で断られた」枝は
final_removed を見ていなかった。

実測（mock の機器: stat が 0o100600、posix_rename が IOError、1 本目の rename が
IOError("Failure")、remove が成功、2 本目の rename が IOError("Permission denied")）:
remove の呼び出しは ['/flash/running.cfg'] で最終名は機器から消えているのに、
文面は「最終名への置き換えに失敗しました。転送済みの内容は機器の一時名
/flash/.running.cfg.netbelt-part.… に残っています: Permission denied」だけで、
remove が断られて最終名が残っているときと 1 文字も違わなかった。利用者は
「元の設定はそのまま」と読むが、実際には消えている。

直し方: この except IOError でも final_removed を見て、消してある場合だけ
「最終名は置き換えの手順で既に消してあります。」を加える（期限切れの枝と
同じ考え）。remove が断られたときの文面は変えない。
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


class SftpRefusedReplaceAfterRemoveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-refused-replace-")
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
        """posix_rename の無い機器で、最終名の remove のあとの rename が断られる"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        attr = mock.Mock()
        attr.st_mode = 0o100600          # 置き換える最終名がある
        self.client.stat.return_value = attr
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        # 1 本目は「既にある」で断られ、remove のあとの 2 本目は権限で断られる
        self.client.rename.side_effect = [IOError("Failure"),
                                          IOError("Permission denied")]
        self.client.remove.side_effect = remove_side_effect
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        self.assertEqual(self.done, [], "失敗なのに完了を通知している")
        tmp = self.client.put.call_args[0][1]
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("最終名への置き換えに失敗しました", self.errors[0],
                      "確定した失敗が伝わらない: %s" % self.errors)
        return tmp

    def test_a_removed_final_name_is_reported_when_the_replace_is_refused(self):
        """remove が成功して最終名が消えているなら、消してあることを伝えること。"""
        self._upload(None)

        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [FINAL],
                         "最終名を消していない前提が崩れている: %s" % removed)
        self.assertIn("既に消してあります", self.errors[0],
                      "最終名が消えているのに残っていると読める: %s" % self.errors)

    def test_a_refused_remove_still_says_nothing_about_the_final_name(self):
        """remove が断られて最終名が残っているなら、「消してあります」と言わないこと。"""
        self._upload(IOError("Permission denied"))

        self.assertNotIn("既に消してあります", self.errors[0],
                         "消えていない最終名を「消した」と言い切っている: %s" % self.errors)


if __name__ == "__main__":
    unittest.main()
