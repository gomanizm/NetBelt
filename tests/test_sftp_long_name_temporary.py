"""ファイルシステムが受け付ける長い名前でも、一時名が長すぎて失敗しないことを検証する。

何が起きていたか（実測）:
  一時名はどちらの向きも元の basename 全体を含んでいた。下り（tempfile.mkstemp、
  prefix=元の名前+'.'、乱数 8 文字、suffix='.netbelt-part'）は元の長さ +22、
  上り（'.%s.netbelt-part.%d-%s'）は元の長さ +24+len(pid)（pid 5 桁で +29）。
  元の名前がファイルシステムの制限（255）内でも、一時名がそれを超える。

  240 文字（'x'*236 + '.cfg'）の保存先で実測:
    元の名前でのローカル作成は成功（長いパスが有効なので総パス 285 文字でも可）
    errors: ["ダウンロードエラー: [Errno 22] Invalid argument: '…xxxx.cfg.udqi8c4v.netbelt-part'"]
    保存先ができたか: False
    アップロードの一時名の basename の長さ: 269（元 240）
  しきい値: 233 文字までは成功（下りの一時名がちょうど 255）、234 文字から失敗。
  エラー文（[Errno 22] Invalid argument と長大なパス）からは原因が分からない。

どう直したか:
  一時名の basename の長さに上限（255）を設け、元の名前は収まるぶんだけ前方を
  残して切り詰める。下りは mkstemp の prefix を、上りは '.%s.netbelt-part.%s' の
  %s を切り詰めた名前にする。元の名前の手掛かりは残す（「一時名 X が残っています」
  の案内から元ファイルを辿れなくなるため、短い識別子だけにはしない）。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

LIMIT = 255
LONG = "x" * 236 + ".cfg"          # 240 文字。実測でどちらの向きも超える
FROM_DEVICE = b"downloaded from device\n"


class SftpLongNameTemporaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-longname-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _wait(self, predicate, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        self.client.stat.side_effect = IOError("No such file")   # 既存は無い

        def get(remote, localpath, callback=None):
            with open(localpath, "wb") as f:
                f.write(FROM_DEVICE)

        self.client.get.side_effect = get
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        return m

    def _uploaded_tmp_basename(self, m, remote_name):
        local = os.path.join(self.dir, "src.cfg")
        with open(local, "wb") as f:
            f.write(b"hostname R1\n")

        m.upload_file(local, "/flash/" + remote_name)

        self.assertTrue(self._wait(lambda: self.done or self.errors),
                        "完了もエラーも届かない")
        self.assertEqual(self.errors, [], "送れなくなっている: %s" % self.errors)
        return os.path.basename(self.client.put.call_args[0][1])

    # --- ダウンロード ---

    def test_a_long_local_name_can_still_be_downloaded(self):
        """ローカルに作れる長さの保存先なら、一時名のせいで失敗しないこと。"""
        local = os.path.join(self.dir, LONG)
        with open(local, "wb") as f:      # その名前自体は作れる
            f.write(b"")
        os.remove(local)
        m = self._manager()

        m.download_file("/flash/" + LONG, local)

        self.assertTrue(self._wait(lambda: self.done or self.errors),
                        "完了もエラーも届かない")
        self.assertEqual(self.errors, [], "長い名前で落とせない: %s" % self.errors)
        with open(local, "rb") as f:
            self.assertEqual(f.read(), FROM_DEVICE)
        self.assertEqual([n for n in os.listdir(self.dir)
                          if n.endswith(".netbelt-part")], [],
                         "一時ファイルが残っている")

    def test_a_long_download_keeps_a_hint_of_the_original_name(self):
        """切り詰めても、一時名から元のファイルを辿れる手掛かりを残すこと。"""
        m = self._manager()
        held = []

        def get(remote, localpath, callback=None):
            held.append(os.path.basename(localpath))
            with open(localpath, "wb") as f:
                f.write(FROM_DEVICE)

        self.client.get.side_effect = get
        m.download_file("/flash/" + LONG, os.path.join(self.dir, LONG))

        self.assertTrue(self._wait(lambda: self.done or self.errors))
        self.assertEqual(self.errors, [], self.errors)
        self.assertEqual(len(held), 1)
        self.assertLessEqual(len(held[0]), LIMIT,
                             "一時名が長さの上限を超えている: %d" % len(held[0]))
        self.assertTrue(held[0].startswith(LONG[:32]),
                        "元の名前の手掛かりが残っていない: %s" % held[0])

    def test_a_short_download_name_is_untouched(self):
        """短い名前は、これまでどおり元の名前をそのまま含むこと。"""
        m = self._manager()
        held = []
        self.client.get.side_effect = lambda r, p, callback=None: (
            held.append(os.path.basename(p)), open(p, "wb").write(FROM_DEVICE))

        m.download_file("/flash/backup.cfg", os.path.join(self.dir, "backup.cfg"))

        self.assertTrue(self._wait(lambda: self.done or self.errors))
        self.assertTrue(held[0].startswith("backup.cfg."), held[0])

    # --- アップロード ---

    def test_a_long_remote_name_gets_a_temporary_name_that_fits(self):
        """上りの一時名も、長さの上限に収めること。"""
        m = self._manager()

        tmp = self._uploaded_tmp_basename(m, LONG)

        self.assertLessEqual(len(tmp), LIMIT,
                             "一時名が長さの上限を超えている: %d 文字" % len(tmp))
        self.assertTrue(tmp.startswith("." + LONG[:32]),
                        "元の名前の手掛かりが残っていない: %s" % tmp)
        self.assertIn(".netbelt-part.", tmp, "一時名の目印が消えている: %s" % tmp)

    def test_a_short_remote_name_is_untouched(self):
        """短い名前の一時名は、これまでどおり元の名前をそのまま含むこと。"""
        m = self._manager()

        tmp = self._uploaded_tmp_basename(m, "running.cfg")

        self.assertTrue(tmp.startswith(".running.cfg.netbelt-part."), tmp)


if __name__ == "__main__":
    unittest.main()
