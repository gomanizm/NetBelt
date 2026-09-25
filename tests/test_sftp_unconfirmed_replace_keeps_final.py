"""上書きの確認を経ていない送信が、代替経路で最終名を消して置き換えないことを検証する。

posix_rename の無い機器向けの復旧手順は「rename が断られたら
remove(最終名) してから rename し直す」だった。ここは overwrite を見て
いない。非 posix の rename は「既存があれば失敗する」ことそのものが安全網
なのに、その失敗を受けて最終名を消しにいくので、安全網を自分で外して
いた。STAT を実装しない機器（無いファイルにも在るファイルにも汎用の失敗を
返す）では送る直前の _remote_probe が「無い」と読むため、この経路は現実に
届く。

実測（mock の機器: stat が IOError("Failure")、posix_rename が IOError、
rename が [IOError("Failure"), 成功]、overwrite=False）: remove の呼び出しは
['/flash/running.cfg'] で、確認を経ていない送信が既存ファイルを消して
置き換えたうえ「アップロード完了: running.cfg」と通知した。

直し方: 上書きの確認を得ている（overwrite が真）ときだけ、最終名を消す
復旧手順へ進む。確認を得ていなければ最終名には触れず、転送した内容が一時名に
残っていることと、上書きなら置き換えられることが分かる文面で断る。

前提の更新（2026-09-20、利用者の承認済み）: 6 周目に「確認を経ていない送信は
posix_rename を使わない」へ変えたので、overwrite=False の場合は posix_rename が
呼ばれない。それでも IOError を仕込んだままだと「posix_rename の無い機器」という
前提が空振りし、見張りとして効かない。確認なしの送信では posix_rename を成功する
ままにして（退行したら既存が黙って置き換わり、完了が通知されて検査が落ちる）、
呼ばれていないことも確かめる。posix_rename の無い機器という前提が要るのは、
最終名を消す復旧手順を見る overwrite=True の検査だけなので、そちらにだけ残す。
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


class SftpUnconfirmedReplaceKeepsFinalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-unconfirmed-")
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

    def _upload(self, overwrite):
        """STAT を実装しない機器へ送り、1 本目の rename が「既にある」で断られる"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        # STAT を実装しない機器。在るファイルにも汎用の失敗を返すので、
        # 送る直前の確認は「無い」と読む
        self.client.stat.side_effect = IOError("Failure")
        if overwrite:
            # 最終名を消す復旧手順を見るので、posix_rename の無い機器にする
            self.client.posix_rename.side_effect = IOError("Operation unsupported")
        # 確認を経ていない送信では posix_rename を成功するままにしておく。
        # 使ってしまう退行が起きたら、既存が黙って置き換わって完了が通知され、
        # 下の検査が落ちる（IOError を仕込むと、呼ばれない前提が空振りする）
        # 最終名は実在するので 1 本目の rename は断られる。消したあとなら通る
        self.client.rename.side_effect = [IOError("Failure"), None]
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)

        m.upload_file(self.local, FINAL, overwrite=overwrite)

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        return m

    def test_an_unconfirmed_upload_does_not_remove_the_final_name(self):
        """確認を経ていない送信は、最終名を消して置き換えないこと。"""
        self._upload(overwrite=False)

        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertNotIn(FINAL, removed,
                         "確認を経ていないのに最終名を消している: %s" % removed)
        self.client.posix_rename.assert_not_called()
        self.assertEqual(self.client.rename.call_count, 1,
                         "最終名を消す復旧手順へ進んでいる")
        self.assertEqual(self.done, [],
                         "置き換えていないのに完了を通知している: %s" % self.done)

    def test_an_unconfirmed_upload_says_where_the_bytes_are(self):
        """断るときは、一時名に残っていることと上書きならできることを伝えること。"""
        self._upload(overwrite=False)

        tmp = self.client.put.call_args[0][1]
        self.assertTrue(self.errors, "断った理由が届かない")
        self.assertIn(tmp, self.errors[0],
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("上書き", self.errors[0],
                      "上書きなら置き換えられることが伝わらない: %s" % self.errors)
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertNotIn(tmp, removed, "唯一の完全な写し（一時名）を消している")

    def test_a_confirmed_upload_still_replaces_through_the_fallback(self):
        """上書きを承認済みなら、これまでどおり最終名を消して置き換えること。"""
        self._upload(overwrite=True)

        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [FINAL],
                         "承認済みの置き換えが通らなくなっている: %s" % removed)
        self.assertEqual(self.client.rename.call_count, 2)
        self.assertTrue(self.done, "完了が通知されない: %s" % self.errors)


if __name__ == "__main__":
    unittest.main()
