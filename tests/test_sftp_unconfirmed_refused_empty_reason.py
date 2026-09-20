"""確認なし送信の改名が理由の無い例外で断られても、文面が「: 」で終わらないことを検証する。

何が起きていたか（実測）:
  上書きの確認を経ていない送信は、最終名へ ふつうの rename を使う。断られた
  ときの文面は `"…やり直してください: %s" % (…, first_error)` で、このリポジトリ
  が 4 か所（_fail、_remote_probe、期限切れの補足、置き換えの復旧手順）で当てて
  いる「str が空なら例外の類名を使う」guard が無かった。paramiko 4.0.0 の
  _convert_status は message を持たない応答でも読み進むので text='' の IOError は
  実在する。mock の機器（stat が IOError("Failure")、rename が IOError("")、
  overwrite 省略）で送ると、通知は
  'アップロードエラー: リモートの …やり直してください: ' と末尾が「: 」だった。
  6 周目に「確認を経ていない送信は posix_rename を使わない」へ変えた結果、ここは
  確認なし送信のいちばん普通の失敗経路になっている。

どう直したか:
  既存の 4 か所と同じ `str(first_error) or first_error.__class__.__name__` にした。
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


class SftpUnconfirmedRefusedEmptyReasonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-unconf-reason-")
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

    def _refused_with(self, error):
        """STAT を実装しない機器へ確認なしで送り、最終名への rename が error で断られる"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        # 在るファイルにも汎用の失敗を返す機器。送る直前の確認は「無い」と読む
        self.client.stat.side_effect = IOError("Failure")
        self.client.rename.side_effect = error
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)

        m.upload_file(self.local, FINAL)    # overwrite は渡さない（確認なし送信）

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.assertTrue(self.errors, "断った理由が届かない")
        return m

    def test_a_reasonless_refusal_still_gives_a_reason(self):
        """理由の読めない IOError でも、文面が理由の無い「: 」で終わらないこと。"""
        self._refused_with(IOError(""))

        self.assertFalse(self.errors[0].rstrip().endswith(":"),
                         "理由が空のまま終わっている: %s" % self.errors)
        self.assertIn("OSError", self.errors[0],
                      "理由の代わりになる例外の類名が出ていない: %s" % self.errors)

    def test_the_tmp_name_and_the_retry_hint_are_still_there(self):
        """理由を補っても、一時名の在処とやり直し方は変わらないこと。"""
        self._refused_with(IOError(""))

        tmp = self.client.put.call_args[0][1]
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("上書き", self.errors[0],
                      "上書きなら置き換えられることが伝わらない: %s" % self.errors)
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertNotIn(tmp, removed, "唯一の完全な写し（一時名）を消している")

    def test_a_refusal_with_a_message_still_shows_it(self):
        """理由が読める例外は、これまでどおりその文言を出すこと。"""
        self._refused_with(IOError("Failure"))

        self.assertIn("Failure", self.errors[0],
                      "読める理由が消えている: %s" % self.errors)


if __name__ == "__main__":
    unittest.main()
