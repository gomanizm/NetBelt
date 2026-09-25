"""転送中に外から作られた保存先を、確認なしで消さないことを検証する。

何が起きていたか（実測）:
  保存ダイアログ（QFileDialog.getSaveFileName）は、その時点で無い名前には
  上書き確認を出さない。それでも転送が終わると download_file は無条件に
  os.replace(一時名, 保存先) を実行していた。_download_targets が見るのは
  同一プロセスの SFTP ダウンロード同士だけなので、SFTP 以外の書き込み
  （別アプリ・別プロセス）は素通りする。

  存在しない backup.cfg を保存先にして download_file を呼び、get() を止めて
  転送中にしたまま別プロセスで同じ名前を作って閉じ、get() を解放して完了
  させると:
    保存先は存在するか（ダイアログの時点）: False
    errors: []  /  done: ['ダウンロード完了: backup.cfg']
    別アプリの内容は残っているか: False
  利用者は一度も上書きを承認しておらず、警告も出ないまま、別アプリが書いた
  内容が消えた。

どう直したか:
  download_file の呼び出し時点（GUI スレッド、保存ダイアログの直後）で保存先の
  有無を控える。そこに無かった＝上書きを承認されていないダウンロードの確定
  処理は os.replace ではなく os.rename にする。Windows の os.rename は宛先が
  あると FileExistsError（WinError 183）で断り、宛先の中身は無傷のままなので、
  「無ければ作る」を原子的に行える。断られたら一時名を消さずに残し、
  アップロード側と同じ形で在処を知らせる。呼び出し時点で既にあった
  （＝ダイアログが上書きを確認した）ダウンロードは、これまでどおり os.replace。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

CREATED_OUTSIDE = "#別アプリが書いた、まだ一度も保存していない内容\n".encode("utf-8")
FROM_DEVICE = b"downloaded from device\n"


class SftpDownloadOutsideWriteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-dl-outside-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "backup.cfg")

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _parts(self):
        return [n for n in os.listdir(self.dir) if n.endswith(".netbelt-part")]

    def _read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def _manager(self, gate=None):
        """get() が中身を書いたあと gate が立つまで止まるマネージャを返す"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        self.started = threading.Event()

        def get(remote, localpath, callback=None):
            with open(localpath, "wb") as f:
                f.write(FROM_DEVICE)
            self.started.set()
            if gate is not None:
                gate.wait(5.0)

        self.client.get.side_effect = get
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        return m

    def _gate(self):
        gate = threading.Event()
        self.addCleanup(gate.set)      # 失敗しても転送スレッドを待たせたままにしない
        return gate

    def test_a_target_created_during_the_transfer_is_not_replaced(self):
        """転送中に外から作られた保存先は、置き換えないこと。"""
        gate = self._gate()
        m = self._manager(gate=gate)

        m.download_file("/flash/backup.cfg", self.local)   # この時点では無い
        self.assertTrue(self.started.wait(5.0), "転送が始まらない")
        with open(self.local, "wb") as f:                  # 外から作られる
            f.write(CREATED_OUTSIDE)
        gate.set()

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.assertEqual(self.done, [],
                         "置き換えていないのに完了を通知している: %s" % self.done)
        self.assertEqual(self._read(self.local), CREATED_OUTSIDE,
                         "承認していない上書きで、外から書かれた内容が消えた")

    def test_the_downloaded_bytes_are_kept_and_the_user_is_told_where(self):
        """置き換えなかった中身は一時名に残し、その在処を知らせること。"""
        gate = self._gate()
        m = self._manager(gate=gate)

        m.download_file("/flash/backup.cfg", self.local)
        self.assertTrue(self.started.wait(5.0))
        with open(self.local, "wb") as f:
            f.write(CREATED_OUTSIDE)
        gate.set()

        self.assertTrue(self._wait(lambda: self.errors), "断った理由が届かない")
        parts = self._parts()
        self.assertEqual(len(parts), 1,
                         "落とした内容を消している（一時名が残っていない）: %s" % parts)
        self.assertEqual(self._read(os.path.join(self.dir, parts[0])), FROM_DEVICE)
        self.assertIn(parts[0], self.errors[0],
                      "残した一時名を知らせていない: %s" % self.errors)
        self.assertIn("置き換えていません", self.errors[0],
                      "置き換えていないことが伝わらない: %s" % self.errors)

    def test_the_target_can_be_used_again_after_the_refusal(self):
        """断ったあとも、同じ保存先の予約は残さないこと。"""
        gate = self._gate()
        m = self._manager(gate=gate)

        m.download_file("/flash/backup.cfg", self.local)
        self.assertTrue(self.started.wait(5.0))
        with open(self.local, "wb") as f:
            f.write(CREATED_OUTSIDE)
        gate.set()
        self.assertTrue(self._wait(lambda: self.errors))

        again = self._manager()
        again.download_file("/flash/backup.cfg", self.local)
        self.assertTrue(self._wait(lambda: self.done),
                        "断った転送の予約が残っている: %s" % self.errors)

    def test_a_target_that_already_existed_is_still_replaced(self):
        """ダイアログで上書きを承認済み（呼び出し時点で在る）なら、置き換えること。"""
        with open(self.local, "wb") as f:
            f.write(b"older backup\n")
        m = self._manager()

        m.download_file("/flash/backup.cfg", self.local)

        self.assertTrue(self._wait(lambda: self.done),
                        "承認済みの上書きが通らなくなっている: %s" % self.errors)
        self.assertEqual(self._read(self.local), FROM_DEVICE)
        self.assertEqual(self._parts(), [], "一時ファイルが残っている")

    def test_an_undisturbed_download_still_lands_on_the_target(self):
        """邪魔が入らなければ、これまでどおり保存先へ置くこと。"""
        m = self._manager()

        m.download_file("/flash/backup.cfg", self.local)

        self.assertTrue(self._wait(lambda: self.done),
                        "ふつうのダウンロードが通らなくなっている: %s" % self.errors)
        self.assertEqual(self._read(self.local), FROM_DEVICE)
        self.assertEqual(self._parts(), [], "一時ファイルが残っている")


if __name__ == "__main__":
    unittest.main()
