"""全ログ保存のキャンセル／失敗で、保存先の既存ファイルを失わないことを検証する。

LogSaveWorker.run は最初に open(file_path, 'w') で保存先を切り詰めてから
書いていた。キャンセル済みでも 0 バイトに切り詰め（実測: 520 → 0 バイト）、
途中キャンセル・途中失敗では新旧どちらでもない部分ファイルが残った
（実測: 1700 → 307500 バイト、[Errno 28] 後に 61500 バイト）。利用者が
既存ファイルを保存先に選んで上書きを承諾した場合、旧内容が黙って消える。

同じディレクトリの一時ファイルへ書き切ってから os.replace で差し替え、
キャンセル・失敗では一時ファイルを消して保存先には触れない。
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


class _FileProxy:
    """書き込みを数えて、指定回数目で「ディスク満杯」を起こす代役。"""

    def __init__(self, inner, fail_at=None):
        self._inner = inner
        self._fail_at = fail_at
        self.writes = 0

    def write(self, text):
        self.writes += 1
        if self._fail_at is not None and self.writes >= self._fail_at:
            raise OSError(28, "No space left on device")
        return self._inner.write(text)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _patched_open(fail_at=None):
    """open() で得た書き込み用ファイルを _FileProxy で包む（読み取りは素通し）。"""
    def fake_open(file, mode="r", *args, **kwargs):
        f = _real_open(file, mode, *args, **kwargs)
        if "w" in mode or "a" in mode:
            return _FileProxy(f, fail_at)
        return f
    return mock.patch("builtins.open", fake_open)


class LogSavePreservesExistingFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netbelt-save-")
        self.target = os.path.join(self.tmp, "existing.log")
        with _real_open(self.target, "w", encoding="utf-8", newline="") as f:
            f.write(ORIGINAL)
        self.log_text = "".join("new line %06d\n" % i for i in range(20000))

    def _read_target(self):
        with _real_open(self.target, "r", encoding="utf-8", newline="") as f:
            return f.read()

    def _leftovers(self):
        return sorted(n for n in os.listdir(self.tmp) if n != "existing.log")

    def _make_worker(self):
        from ui.dialogs.log_save_dialog import LogSaveWorker
        worker = LogSaveWorker(self.log_text, self.target)
        results = []
        worker.finished.connect(lambda ok, msg: results.append((ok, msg)))
        return worker, results

    def _pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def test_cancelled_before_start_leaves_the_file_untouched(self):
        """キャンセル済みのワーカーは保存先を切り詰めない（実測: 520 → 0 バイト）。"""
        worker, results = self._make_worker()
        worker.cancel()
        worker.start()
        self.assertTrue(worker.wait(5000))
        self._pump()

        self.assertEqual(results, [(False, "キャンセルされました")])
        self.assertEqual(self._read_target(), ORIGINAL,
                         "キャンセル済みなのに保存先が書き換えられた")
        self.assertEqual(self._leftovers(), [], "一時ファイルが残っている")

    def test_cancelled_midway_leaves_the_old_contents(self):
        """途中キャンセルでも旧内容がそのまま残り、部分ファイルにならないこと。"""
        worker, results = self._make_worker()
        # run() を直接呼ぶと progress は同じスレッドで直結に届くので、
        # 最初のチャンクの直後に確実にキャンセルできる
        worker.progress.connect(lambda percent: worker.cancel())
        worker.run()

        self.assertEqual(results, [(False, "キャンセルされました")])
        self.assertEqual(self._read_target(), ORIGINAL,
                         "途中キャンセルで保存先が部分ファイルになった")
        self.assertEqual(self._leftovers(), [], "一時ファイルが残っている")

    def test_write_failure_midway_leaves_the_old_contents(self):
        """途中で書き込みに失敗しても旧内容が残り、失敗が通知されること。"""
        worker, results = self._make_worker()
        with _patched_open(fail_at=3):
            worker.run()

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertIn("No space left on device", results[0][1])
        self.assertEqual(self._read_target(), ORIGINAL,
                         "書き込み失敗で保存先が部分ファイルになった")
        self.assertEqual(self._leftovers(), [], "一時ファイルが残っている")

    def test_successful_save_replaces_the_file(self):
        """成功時はこれまでどおり保存先が新しい内容になり、一時ファイルも残らないこと。"""
        worker, results = self._make_worker()
        worker.start()
        self.assertTrue(worker.wait(5000))
        self._pump()

        self.assertEqual(results, [(True, "")])
        # テキストモードの改行変換（Windows では \r\n）は従来どおり
        self.assertEqual(self._read_target().replace("\r\n", "\n"), self.log_text)
        self.assertEqual(self._leftovers(), [], "一時ファイルが残っている")


if __name__ == "__main__":
    unittest.main()
