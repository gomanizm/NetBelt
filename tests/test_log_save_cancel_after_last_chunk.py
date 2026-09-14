"""最終チャンクを書き終えたあとのキャンセルでも、保存先を置き換えないことを検証する。

LogSaveWorker.run のキャンセル判定は書き込みループの先頭だけだった。最後の
チャンクを書いてループを抜けたあとに cancel() が立っても、そのまま
os.replace が走って保存先が差し替わる（実測: 既存の 600 バイトが新内容へ）。
「キャンセル・失敗では一時ファイルを消して保存先には触れない」という
tests/test_log_save_preserves_existing_file.py の契約に対する穴。

置き換える直前にもう一度キャンセルを見る。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ORIGINAL = "old contents that must survive\n" * 20


class LogSaveCancelAfterLastChunkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netbelt-save-late-")
        self.target = os.path.join(self.tmp, "existing.log")
        with open(self.target, "w", encoding="utf-8", newline="") as f:
            f.write(ORIGINAL)
        self.log_text = "".join("new line %06d\n" % i for i in range(20000))

    def _read_target(self):
        with open(self.target, "r", encoding="utf-8", newline="") as f:
            return f.read()

    def _leftovers(self):
        return sorted(n for n in os.listdir(self.tmp) if n != "existing.log")

    def test_cancel_after_the_last_chunk_does_not_replace_the_target(self):
        """最後のチャンクを書き終えたあとのキャンセルでも旧内容が残ること。"""
        from ui.dialogs.log_save_dialog import LogSaveWorker

        worker = LogSaveWorker(self.log_text, self.target)
        results = []
        worker.finished.connect(lambda ok, msg: results.append((ok, msg)))
        # run() を直接呼ぶと progress は同じスレッドで直結に届く。
        # 100% は最終チャンクの write 直後にしか出ないので、
        # 「書き切ったあと・置き換える前」のキャンセルを再現できる。
        worker.progress.connect(
            lambda percent: worker.cancel() if percent >= 100 else None)
        worker.run()

        self.assertEqual(results, [(False, "キャンセルされました")])
        self.assertEqual(self._read_target(), ORIGINAL,
                         "キャンセル後に保存先が置き換えられた")
        self.assertEqual(self._leftovers(), [], "一時ファイルが残っている")


if __name__ == "__main__":
    unittest.main()
