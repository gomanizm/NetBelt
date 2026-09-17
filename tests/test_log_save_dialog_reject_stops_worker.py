"""保存ダイアログを Esc／× で閉じても、ワーカーが止まることを検証する。

LogSaveProgressDialog はキャンセルボタンだけが worker.cancel()+wait() を
通していた。Esc や × は QDialog.reject() へ直行するので、進捗表示だけが
消えて裏では書き込みが続く（実測: exec() が戻った直後も isRunning()=True、
ファイルは 102500 → 512500 バイトと増え続け、その直後にアプリを終了すると
途中で切れたファイルが黙って残る）。

どの閉じ方でも reject() を通るので、そこでワーカーを止めて待つ。
"""
import builtins
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

ORIGINAL = "old contents that must survive\n" * 20
_real_open = builtins.open


class _SlowFile:
    """1 回の書き込みに時間がかかるファイルの代役（低速ディスクを模す）。"""

    def __init__(self, inner, delay):
        self._inner = inner
        self._delay = delay

    def write(self, text):
        time.sleep(self._delay)
        return self._inner.write(text)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _slow_open(delay):
    def fake_open(file, mode="r", *args, **kwargs):
        f = _real_open(file, mode, *args, **kwargs)
        if "w" in mode:
            return _SlowFile(f, delay)
        return f
    return mock.patch("builtins.open", fake_open)


class LogSaveDialogRejectStopsWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netbelt-savedlg-")
        self.target = os.path.join(self.tmp, "existing.log")
        with _real_open(self.target, "w", encoding="utf-8", newline="") as f:
            f.write(ORIGINAL)
        # 100 チャンク × 50ms なので、止めなければ 5 秒かかる
        self.log_text = "".join("new line %06d\n" % i for i in range(20000))
        patcher = _slow_open(0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _open_dialog(self):
        from ui.dialogs.log_save_dialog import LogSaveProgressDialog
        dialog = LogSaveProgressDialog(self.log_text, self.target, None)
        self.addCleanup(lambda: dialog.worker and dialog.worker.wait(10000))
        dialog.show()
        self._pump(0.2)
        self.assertTrue(dialog.worker.isRunning(), "前提: 保存が走っている")
        return dialog

    def _assert_stopped(self, dialog, how):
        worker = dialog.worker
        self.assertTrue(worker._is_cancelled,
                        "%s でもワーカーにキャンセルが伝わっていない" % how)
        self.assertFalse(worker.isRunning(),
                         "%s の後もワーカーが書き込みを続けている" % how)
        self.assertFalse(dialog.isVisible())
        self._pump(0.1)
        self.assertFalse(dialog._success, "止めた保存が成功扱いになっている")

    def test_escape_key_stops_the_worker(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        dialog = self._open_dialog()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self._assert_stopped(dialog, "Esc")

    def test_close_button_stops_the_worker(self):
        dialog = self._open_dialog()
        dialog.close()      # タイトルバーの × と同じ closeEvent 経路
        self._assert_stopped(dialog, "×")

    def test_cancel_button_still_stops_the_worker(self):
        dialog = self._open_dialog()
        dialog.cancel_button.click()
        self._assert_stopped(dialog, "キャンセルボタン")


if __name__ == "__main__":
    unittest.main()
