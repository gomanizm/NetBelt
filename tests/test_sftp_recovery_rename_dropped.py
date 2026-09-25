"""最終名を消したあとの改名が切断で落ちたとき、失敗と断定しないことを検証する。

何が起きていたか（実測）:
  posix_rename の無い機器で置き換えるときは、1 本目の rename が「既にある」で
  断られたら最終名を remove してもう一度 rename する。この 3 本目の except は
  例外の種類を見ずに『最終名への置き換えに失敗しました。最終名は置き換えの手順で
  既に消してあります。転送済みの内容は機器の一時名 … に残っています』と断定して
  いた。サーバ側で改名が適用されてから応答だけが落ちた場合、実際には最終名に
  新しい内容があり一時名は存在しない。利用者は両方失ったと読み、無事な機器を
  復旧しにいく。

  mock の機器（overwrite=True、posix_rename=IOError('Operation unsupported')、
  rename=[IOError('Failure'), SSHException('Server connection dropped: ')]、
  remove 成功）で実測:
    errors: ['アップロードエラー: 最終名への置き換えに失敗しました。最終名は
    置き換えの手順で既に消してあります。転送済みの内容は機器の一時名
    /flash/.running.cfg.netbelt-part.… に残っています: Server connection dropped: ']
  素の EOFError() でも同じ文面だった。すぐ上の同じ呼び出しの TimeoutError の枝は
  『確かめられませんでした』なので、同じ場所で扱いが割れていた。

どう直したか:
  3 本目の except を、機器が答えた失敗（IOError）と、切断・壊れた応答
  （EOFError / SFTPError / SSHException）に割った。前者はこれまでどおりの断定文、
  後者は期限切れの枝と同じ unknown_outcome_error（final_removed=True）にする。
  一時名を残す（keep_tmp）点は元から同じなので、変わるのは文面だけ。
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
UNSURE = "確かめられませんでした"
GONE = "最終名は置き換えの手順で既に消してあります"
ASSERTED = "最終名への置き換えに失敗しました"


class SftpRecoveryRenameDroppedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-recovery-dropped-")
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

    def _recovery_rename_fails_with(self, error):
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
        self.assertEqual(self.done, [],
                         "置き換えたか分からないのに完了を通知している: %s" % self.done)
        return " / ".join(self.errors)

    def _assert_unsure(self, joined):
        tmp = self.client.put.call_args[0][1]
        self.assertIn(UNSURE, joined,
                      "置き換わったか不明であることが伝わらない: %s" % joined)
        self.assertNotIn(ASSERTED, joined,
                         "適用済みかもしれないのに失敗と断定している: %s" % joined)
        self.assertIn(GONE, joined,
                      "最終名を消してあることが伝わらない: %s" % joined)
        self.assertIn(tmp, joined, "一時名の在処を知らせていない: %s" % joined)
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertNotIn(tmp, removed,
                         "転送した唯一の写し（一時名）を消している: %s" % removed)

    def test_a_dropped_connection_after_the_final_name_was_removed_is_not_a_failure(self):
        """切断（SSHException）では、置き換わったか不明として伝えること。"""
        joined = self._recovery_rename_fails_with(
            paramiko.SSHException("Server connection dropped: "))

        self._assert_unsure(joined)
        self.assertIn("Server connection dropped", joined,
                      "落ちた理由が伝わらない: %s" % joined)

    def test_a_bare_eof_after_the_final_name_was_removed_is_not_a_failure(self):
        """素の EOFError（str が空）でも同じで、理由は例外の類名で補うこと。"""
        joined = self._recovery_rename_fails_with(EOFError())

        self._assert_unsure(joined)
        self.assertIn("EOFError", joined,
                      "理由の代わりになる例外の類名が出ていない: %s" % joined)

    def test_a_broken_response_after_the_final_name_was_removed_is_not_a_failure(self):
        """壊れた応答（SFTPError）でも同じであること。"""
        joined = self._recovery_rename_fails_with(
            paramiko.SFTPError("Garbage packet received"))

        self._assert_unsure(joined)

    def test_a_refusal_from_the_device_is_still_a_settled_failure(self):
        """機器が答えた失敗（IOError）は、これまでどおり断定してよいこと。"""
        joined = self._recovery_rename_fails_with(IOError("Permission denied"))

        tmp = self.client.put.call_args[0][1]
        self.assertIn(ASSERTED, joined, "確定した失敗の文面が変わっている: %s" % joined)
        self.assertIn(GONE, joined, "最終名を消してあることが伝わらない: %s" % joined)
        self.assertIn(tmp, joined, "一時名の在処を知らせていない: %s" % joined)
        self.assertIn("Permission denied", joined, "理由が消えている: %s" % joined)


if __name__ == "__main__":
    unittest.main()
