"""期限切れが重なっても、切断の通知が 1 回で済むことを検証する。

送りかけの一時名を片づける remove が期限切れになったら接続を畳む
（test_sftp_cleanup_remove_timeout.py）。その印（probe_timed_out）は
finally の _fail が読むが、元の失敗そのものが期限切れだった場合は
except 節の _fail も期限切れとして畳むので、_fail が 2 回走っていた。
死んだチャンネルでは put が期限切れなら後始末の remove も期限切れになる
ので、この重なりは狙った組み合わせより現実に起きやすい。

実測（mock の機器: stat が 0o100600、put が TimeoutError、後始末の remove も
TimeoutError、overwrite=True）: 同じ「機器が30秒応答しません。SFTP接続を
切断しました」が 2 件届き、disconnected も 2 回出た（重なりが無ければ
どちらも 1 回）。利用者には切断が 2 回起きたように見え、パネル側の
切断処理も二度走る。同じ重なりは、転送前の _create_tmp_with_mode が
期限切れになる経路でも起きる。

直し方: 後始末の remove が期限切れになったとき、元の例外が既に期限切れ
なら probe_timed_out / timed_out_note を立てない。片づけられなかった
一時名は、その 1 回きりの文面に添えて伝える。
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


class SftpDoubleDisconnectNoticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-double-")
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

    def _upload(self, stat_effect, put_effect):
        """置き換えが失敗し、送りかけの一時名の後始末も期限切れになる"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        # 切断すると m.sftp_client は None になるので、呼び出しの記録は控えて見る
        self.client = m.sftp_client = mock.Mock()
        attr = mock.Mock()
        attr.st_mode = 0o100600          # 置き換える最終名がある
        self.client.stat.return_value = attr
        self.client.stat.side_effect = stat_effect
        self.client.put.side_effect = put_effect
        self.client.remove.side_effect = TimeoutError()   # 後始末も期限切れ
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.disconnected = [], []
        m.error_occurred.connect(self.errors.append)
        m.disconnected.connect(lambda: self.disconnected.append(True))

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: not m.is_connected),
                        "期限切れのあとも接続中のまま残っている: %s" % self.errors)
        # 二重化は後から届くので、落ち着くまで見てから数える
        self._wait(lambda: False, seconds=0.5)
        return m

    def _assert_told_once(self, tmp):
        self.assertEqual(len(self.errors), 1,
                         "切断の通知が重なっている: %s" % self.errors)
        self.assertEqual(len(self.disconnected), 1,
                         "disconnected が %d 回出ている" % len(self.disconnected))
        self.assertIn("SFTP接続を切断しました", self.errors[0],
                      "切断したことが伝わらない: %s" % self.errors)
        self.assertIn(tmp, self.errors[0],
                      "片づけられなかった一時名を知らせていない: %s" % self.errors)

    def test_a_timed_out_transfer_and_cleanup_are_told_once(self):
        """put も後始末も期限切れなら、切断の通知は 1 回にまとめること。"""
        self._upload(stat_effect=None, put_effect=TimeoutError())

        tmp = self.client.put.call_args[0][1]
        self._assert_told_once(tmp)

    def test_a_timed_out_preparation_and_cleanup_are_told_once(self):
        """転送前の一時名づくりが期限切れになった場合も同じであること。"""
        self._upload(stat_effect=TimeoutError(), put_effect=None)

        self.client.put.assert_not_called()
        tmp = self.client.remove.call_args[0][0]
        self.assertIn(".running.cfg.netbelt-part.", tmp,
                      "後始末が一時名以外を消しにいっている: %s" % tmp)
        self._assert_told_once(tmp)

    def test_a_non_timeout_failure_still_reports_both(self):
        """元の失敗が期限切れでなければ、これまでどおり理由と切断の両方を伝えること。"""
        self._upload(stat_effect=None, put_effect=IOError("Failure"))

        tmp = self.client.put.call_args[0][1]
        self.assertEqual(len(self.disconnected), 1,
                         "disconnected が %d 回出ている" % len(self.disconnected))
        self.assertEqual(len(self.errors), 2,
                         "元の失敗と切断の両方が届かない: %s" % self.errors)
        self.assertIn("アップロードエラー: Failure", self.errors[0],
                      "元の失敗の文面が変わっている: %s" % self.errors)
        self.assertIn(tmp, self.errors[1],
                      "片づけられなかった一時名を知らせていない: %s" % self.errors)


if __name__ == "__main__":
    unittest.main()
