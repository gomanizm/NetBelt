"""同じ保存先へのダウンロードが重なったとき、確認なしで片方が消えないことを検証する。

何が起きていたか（実測）:
  保存ダイアログより後に、保存先の予約も競合の検査も無く、転送が終わると
  一時名から保存先へ無条件に os.replace していた。まだ存在しない同じ
  保存先へ、最初の転送が終わる前に別の機器からもダウンロードを始めると、
  どちらの保存ダイアログにも上書きの確認は出ない（その時点では無い）。
  両方が一時名へ落として順に置き換えるので、後から終わった方が先の方を
  黙って消した。

利用者の決定（2026-09-20）:
  同じ保存先（同じ実パス。大文字小文字や短縮名の違いも同じとみなす）への
  ダウンロードが進行中なら、2 件目を『同じ保存先へのダウンロードが
  進行中です』と伝えて断る。進行中の転送が終わった（成功・失敗・中断）
  ら予約を外す。

どう実装したか:
  SFTPManager に、進行中のダウンロードの保存先をプロセス全体で 1 つ持つ
  （機器ごとにマネージャが別なので、インスタンスをまたいで共有する）。
  鍵は os.path.realpath で短縮名（8.3 形式）を解き、os.path.normcase で
  大文字小文字と区切り文字の違いをならしたもの。download_file は一時名を
  作る前に予約し、既に予約があれば error_occurred で断って何もしない。
  一時名を作れなかったときはその場で、転送が終わったときは成功・失敗・
  中断のどれでも、完了や失敗を通知する前に予約を外す。
"""
import ctypes
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

BUSY = "同じ保存先へのダウンロードが進行中です"


class SftpDownloadSameTargetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-dl-same-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _parts(self, directory=None):
        return [n for n in os.listdir(directory or self.dir)
                if n.endswith(".netbelt-part")]

    def _device(self, content, gate=None, fail=None):
        """1 台ぶんのマネージャと、その通知を集めた箱を返す。

        Args:
            content: get() が保存先へ書く中身
            gate: 渡すと、get() が中身を書く前にこれが立つまで待つ
            fail: 渡すと、get() が途中まで書いてからこの例外を投げる
        """
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        box = {"errors": [], "done": [], "started": threading.Event()}

        def get(remote, localpath, callback=None):
            box["started"].set()
            if gate is not None:
                gate.wait(5.0)
            with open(localpath, "wb") as f:
                f.write(content)
            if fail is not None:
                raise fail

        m.sftp_client.get.side_effect = get
        m.error_occurred.connect(box["errors"].append)
        m.transfer_complete.connect(box["done"].append)
        return m, box

    def _held(self):
        gate = threading.Event()
        self.addCleanup(gate.set)      # 失敗しても転送スレッドを待たせたままにしない
        return gate

    def _read(self, path):
        with open(path, "rb") as f:
            return f.read()

    # --- 進行中なら断る ---

    def test_a_second_download_to_the_same_path_is_refused_while_the_first_runs(self):
        """別の機器から同じ保存先へ落とそうとしたら、2 件目を断ること。"""
        gate = self._held()
        a, box_a = self._device(b"from rtrA", gate=gate)
        b, box_b = self._device(b"from rtrB")
        local = os.path.join(self.dir, "running.cfg")

        a.download_file("/flash/running.cfg", local)
        self.assertTrue(box_a["started"].wait(5.0), "1 件目が始まらない")
        b.download_file("/flash/running.cfg", local)

        self.assertTrue(self._wait(lambda: box_b["errors"]),
                        "2 件目が断られずに始まった（後の置き換えで一方が消える）")
        self.assertIn(BUSY, box_b["errors"][0])
        b.sftp_client.get.assert_not_called()

        gate.set()
        self.assertTrue(self._wait(lambda: box_a["done"]),
                        "1 件目が終わらない: %s" % box_a["errors"])
        self.assertEqual(self._read(local), b"from rtrA")
        self.assertEqual(self._parts(), [], "一時ファイルが残っている")

    def test_the_same_path_in_another_case_is_the_same_target(self):
        """大文字小文字が違うだけの保存先も、同じ保存先として断ること。"""
        gate = self._held()
        a, box_a = self._device(b"from rtrA", gate=gate)
        b, box_b = self._device(b"from rtrB")

        a.download_file("/flash/running.cfg", os.path.join(self.dir, "running.cfg"))
        self.assertTrue(box_a["started"].wait(5.0))
        b.download_file("/flash/running.cfg",
                        os.path.join(self.dir.upper(), "RUNNING.CFG"))

        self.assertTrue(self._wait(lambda: box_b["errors"]), "大文字の保存先が通った")
        self.assertIn(BUSY, box_b["errors"][0])
        b.sftp_client.get.assert_not_called()

    def test_a_short_name_of_the_folder_is_the_same_target(self):
        """フォルダを短縮名（8.3 形式）で指しても、同じ保存先として断ること。"""
        folder = os.path.join(self.dir, "a long folder name for backups")
        os.makedirs(folder)
        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(folder, buf, len(buf))
        if not buf.value or os.path.basename(buf.value) == os.path.basename(folder):
            self.skipTest("このボリュームでは短縮名が作られない")

        gate = self._held()
        a, box_a = self._device(b"from rtrA", gate=gate)
        b, box_b = self._device(b"from rtrB")
        a.download_file("/flash/running.cfg", os.path.join(folder, "running.cfg"))
        self.assertTrue(box_a["started"].wait(5.0))
        b.download_file("/flash/running.cfg", os.path.join(buf.value, "running.cfg"))

        self.assertTrue(self._wait(lambda: box_b["errors"]), "短縮名の保存先が通った")
        self.assertIn(BUSY, box_b["errors"][0])
        b.sftp_client.get.assert_not_called()

    def test_downloads_to_different_paths_run_side_by_side(self):
        """保存先が違えば、進行中の転送があっても断らないこと。"""
        gate = self._held()
        a, box_a = self._device(b"from rtrA", gate=gate)
        b, box_b = self._device(b"from rtrB")

        a.download_file("/flash/running.cfg", os.path.join(self.dir, "rtrA.cfg"))
        self.assertTrue(box_a["started"].wait(5.0))
        b.download_file("/flash/running.cfg", os.path.join(self.dir, "rtrB.cfg"))

        self.assertTrue(self._wait(lambda: box_b["done"]),
                        "別の保存先なのに断られた: %s" % box_b["errors"])
        self.assertEqual(box_b["errors"], [])

    # --- 終わったら予約を外す ---

    def test_the_target_is_free_again_once_the_download_has_finished(self):
        """完了を受け取った直後に、同じ保存先へ落とし直せること。"""
        a, box_a = self._device(b"from rtrA")
        b, box_b = self._device(b"from rtrB")
        local = os.path.join(self.dir, "running.cfg")

        a.download_file("/flash/running.cfg", local)
        self.assertTrue(self._wait(lambda: box_a["done"]), "1 件目が終わらない")
        b.download_file("/flash/running.cfg", local)

        self.assertTrue(self._wait(lambda: box_b["done"]),
                        "終わった転送の予約が残っている: %s" % box_b["errors"])
        self.assertEqual(self._read(local), b"from rtrB")

    def test_the_target_is_free_again_after_a_failed_download(self):
        """途中で失敗（切断）した転送のあとも、同じ保存先へ落とし直せること。"""
        a, box_a = self._device(b"partial", fail=OSError("Socket is closed"))
        b, box_b = self._device(b"from rtrB")
        local = os.path.join(self.dir, "running.cfg")

        a.download_file("/flash/running.cfg", local)
        self.assertTrue(self._wait(lambda: box_a["errors"]), "失敗が通知されない")
        b.download_file("/flash/running.cfg", local)

        self.assertTrue(self._wait(lambda: box_b["done"]),
                        "失敗した転送の予約が残っている: %s" % box_b["errors"])
        self.assertEqual(self._read(local), b"from rtrB")

    def test_the_target_is_free_again_after_a_download_cancelled_while_queued(self):
        """順番待ちのあいだに切断されて中断した転送のあとも、落とし直せること。"""
        a, box_a = self._device(b"from rtrA")
        b, box_b = self._device(b"from rtrB")
        local = os.path.join(self.dir, "running.cfg")

        a._sftp_lock.acquire()          # 先行する転送がロックを握っている
        try:
            a.download_file("/flash/running.cfg", local)
            self.assertTrue(self._wait(lambda: self._parts()))
            a.is_connected = False      # 順番待ちのあいだに切断される
        finally:
            a._sftp_lock.release()
        self.assertTrue(self._wait(lambda: box_a["errors"]), "中断が通知されない")
        b.download_file("/flash/running.cfg", local)

        self.assertTrue(self._wait(lambda: box_b["done"]),
                        "中断した転送の予約が残っている: %s" % box_b["errors"])

    def test_the_target_is_not_left_reserved_when_the_temp_file_cannot_be_made(self):
        """一時名を作れずに始まらなかったダウンロードは、予約を残さないこと。"""
        a, box_a = self._device(b"from rtrA")
        folder = os.path.join(self.dir, "not-yet")
        local = os.path.join(folder, "running.cfg")

        a.download_file("/flash/running.cfg", local)
        self.assertTrue(self._wait(lambda: box_a["errors"]))
        self.assertNotIn(BUSY, box_a["errors"][0])

        os.makedirs(folder)
        a.download_file("/flash/running.cfg", local)
        self.assertTrue(self._wait(lambda: box_a["done"]),
                        "始まらなかった転送の予約が残っている: %s" % box_a["errors"])


if __name__ == "__main__":
    unittest.main()
