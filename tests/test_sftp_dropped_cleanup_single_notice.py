"""切断で失敗した転送の後始末が期限切れでも、通知が 1 回で済むことを検証する。

このリポジトリは「切断の通知は 1 回」を契約にしている
（tests/test_sftp_double_disconnect_notice.py の冒頭、および
tests/test_sftp_dropped_connection_disconnect.py の _assert_folded が
len(gone)==1 を見ている）。その畳み込みが、_fail が切断でも接続を畳む
ようになった枝（利用者の決定 2026-09-20: 切断・壊れた応答も期限切れと
同じく接続を畳む）では破れていた。

実測（mock の機器: stat=IOError('No such file')、
put=paramiko.SSHException('Server connection dropped: ')、後始末の
remove=TimeoutError、overwrite=False）: errors 2 件 →
『アップロードエラー: Server connection dropped。SFTP接続を切断しました。
接続し直してください』と『アップロードエラー（送りかけの一時名 … を
片づけられませんでした）: 機器が30秒応答しません。SFTP接続を切断しました。
接続し直してください』、disconnected emits も 2 回。利用者には切断が
2 回起きたように見え、パネル側の切断処理も二度走る。

原因は upload_thread の except 内、後始末の remove が期限切れになったとき
の分岐が「元の失敗そのものが期限切れのときだけ」二重化を避けていたこと。
_fail が切断でも畳むようになった今は、切断が原因の失敗でも同じ重なりが
起きる。既存テストは remove を素の Mock（成功）にしているので、この
組み合わせを通らなかった。同じ重なりは、転送前の _create_tmp_with_mode が
切断で落ちる経路でも成立する。

直し方: 後始末の remove が期限切れになったとき、元の例外が期限切れ
「または切断・壊れた応答」なら probe_timed_out / timed_out_note を
立てず、片づけられなかった一時名を元の失敗の文面へ添えて 1 回で伝える。
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
DROPPED = "Server connection dropped: "


class SftpDroppedCleanupSingleNoticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-dropped-cleanup-")
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

    def _upload(self, overwrite, stat_effect, put_effect=None,
                open_effect=None):
        """転送が切断で落ち、送りかけの一時名の後始末も期限切れになる"""
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
        self.client.open.side_effect = open_effect
        self.client.remove.side_effect = TimeoutError()   # 後始末は期限切れ
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.disconnected = [], []
        m.error_occurred.connect(self.errors.append)
        m.disconnected.connect(lambda: self.disconnected.append(True))

        m.upload_file(self.local, FINAL, overwrite=overwrite)

        self.assertTrue(self._wait(lambda: not m.is_connected),
                        "切断のあとも接続中のまま残っている: %s" % self.errors)
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

    def test_a_dropped_transfer_and_timed_out_cleanup_are_told_once(self):
        """put が切断で落ち、後始末が期限切れでも、通知は 1 回にまとめること。"""
        self._upload(overwrite=False, stat_effect=IOError("No such file"),
                     put_effect=paramiko.SSHException(DROPPED))

        tmp = self.client.put.call_args[0][1]
        self._assert_told_once(tmp)
        self.assertIn("Server connection dropped", self.errors[0],
                      "元の失敗の理由が消えている: %s" % self.errors)

    def test_a_dropped_preparation_and_timed_out_cleanup_are_told_once(self):
        """転送前の一時名づくりが切断で落ちた場合も同じであること。"""
        self._upload(overwrite=True, stat_effect=None,
                     open_effect=paramiko.SSHException(DROPPED))

        self.client.put.assert_not_called()
        tmp = self.client.remove.call_args[0][0]
        self.assertIn(".running.cfg.netbelt-part.", tmp,
                      "後始末が一時名以外を消しにいっている: %s" % tmp)
        self._assert_told_once(tmp)


if __name__ == "__main__":
    unittest.main()
