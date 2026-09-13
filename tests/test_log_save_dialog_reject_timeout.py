"""応答しない保存先でも、閉じる操作が GUI を固めたままにしないことを検証する。

LogSaveProgressDialog.reject() は worker.cancel() のあと worker.wait() を
タイムアウト無しで呼んでいた。ワーカーのキャンセル判定はチャンクの切れ目
だけなので、1 回の write が返ってこない保存先（応答しない共有フォルダなど）
では wait() が返らず、GUI スレッドごと固まる。

保存先の実体は一時ファイルで、差し替えは書き切ったあとの os.replace 1回
だけなので、待つのをやめても保存先のファイルは壊れない。待ち切れないときは
待つのをやめ、ワーカーは終わるまで別に保持する（実行中の QThread の参照を
落とすとプロセスごと落ちるため）。
"""
import gc
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

from PyQt6.QtCore import QThread, pyqtSignal

# 固まったワーカーを放置する時間。テストの待ち上限より十分長くする。
HANG_SECONDS = 10.0


class _HangingWorker(QThread):
    """write が返ってこない保存先の代役。

    キャンセル要求は受け取るが、チャンクの切れ目まで到達しないので止まらない。
    """

    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)

    def __init__(self, log_text: str, file_path: str):
        super().__init__()
        self.log_text = log_text
        self.file_path = file_path
        self._is_cancelled = False
        self.released = threading.Event()

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        self.released.wait(HANG_SECONDS)


class LogSaveDialogRejectTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netbelt-savedlg-timeout-")
        self.target = os.path.join(self.tmp, "hangs.log")

    def _release(self, worker):
        worker.released.set()
        worker.wait(5000)

    def _open_dialog(self):
        from ui.dialogs import log_save_dialog
        with mock.patch.object(log_save_dialog, "LogSaveWorker", _HangingWorker):
            dialog = log_save_dialog.LogSaveProgressDialog("log text", self.target, None)
        worker = dialog.worker
        self.addCleanup(self._release, worker)

        deadline = time.monotonic() + 5.0
        while not worker.isRunning() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(worker.isRunning(), "前提: 固まったワーカーが走っている")
        return dialog, worker

    def _reject_within_timeout(self, dialog):
        """reject() が有限時間で戻ることを確かめ、かかった秒数を返す。"""
        start = time.monotonic()
        dialog.reject()
        elapsed = time.monotonic() - start
        self.assertLess(
            elapsed, 5.0,
            "reject() が %.1f 秒戻らなかった（止まらないワーカーを無期限に待っている）"
            % elapsed)
        return elapsed

    def test_reject_gives_up_waiting_for_a_worker_that_never_stops(self):
        dialog, worker = self._open_dialog()
        self._reject_within_timeout(dialog)
        self.assertTrue(worker._is_cancelled, "ワーカーにキャンセルが伝わっていない")
        self.assertFalse(dialog.isVisible())

    def test_abandoned_worker_outlives_the_dialog(self):
        dialog, worker = self._open_dialog()
        self._reject_within_timeout(dialog)

        # 実行中の QThread への参照が消えると C++ 側が破棄されて落ちる。
        # ダイアログを手放しても、ワーカーはどこかに保持されていること。
        del dialog
        gc.collect()
        self.app.processEvents()
        self.assertTrue(worker.isRunning(), "待ち切れなかったワーカーが破棄された")

        worker.released.set()
        self.assertTrue(worker.wait(5000), "解放後もワーカーが終わらない")


if __name__ == "__main__":
    unittest.main()
