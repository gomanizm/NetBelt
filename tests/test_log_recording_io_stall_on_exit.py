"""アプリを閉じるとき、詰まった記録は上限（5 秒）まで待ち、書き切れなければ知らせて閉じることを検証する（termui-03 Q2）。

何が起きていたか（441ea02 で実測。scratchpad\\cx132-termlog\\repro\\
repro_termui03_io_stall.py）: 記録の書き込みと close は GUI スレッドで同期に
走っていた。記録先が応答しないまま閉じると、finish_log_recordings（アプリの
終了・更新での終了で呼ぶ）が close の中で止まり、応答が返るまで（SMB の時間切れ
しだいで数十秒以上）ウィンドウが閉じない。

利用者の決定（Q2 (a)）: 記録がまだ詰まっていたら上限 5 秒まで待ち、書き切れな
ければ『記録が途中までの可能性があります』と警告して閉じる。

どう直したか: 書き込みは記録ごとのスレッドへ移した（core/log_writer.py）。
finish_log_recordings は全部の記録を止めたあと、閉じ終わっていない記録の
スレッドを合わせて LOG_FINISH_WAIT（5 秒）まで待ち、残ったものを 1 回の警告で
知らせて戻る。書き終われば、待つのは書き終わるまでで、警告は出さない。
"""
import builtins
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _StallingFile:
    """放されるまで write / flush / close が戻らない記録ファイル（応答しない共有フォルダの代わり）。

    放されないまま limit 秒たったら戻る（直す前の作りでテストが止まったままに
    ならないように）。
    """

    def __init__(self, f, release, limit):
        self._f = f
        self.name = f.name
        self._release = release
        self._limit = limit

    def write(self, text):
        self._release.wait(self._limit)
        return self._f.write(text)

    def flush(self):
        self._release.wait(self._limit)
        return self._f.flush()

    def close(self):
        self._release.wait(self._limit)
        return self._f.close()

    @property
    def closed(self):
        return self._f.closed


def _pump(ms):
    """ms ミリ秒だけイベントループを回す。"""
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class LogRecordingIoStallOnExitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-exit-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _recording(self, release, limit):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        path = os.path.join(self.dir, "rtrA.log")
        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if isinstance(file, str) and file == path and "w" in mode:
                return _StallingFile(f, release, limit)
            return f

        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn("rtrA", w._log_files, "前提: 記録が始まっている")
        return w, path

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_closing_the_app_waits_five_seconds_at_most_and_warns(self):
        """書き切れない記録があっても、終了は 5 秒ほどで戻り、途中までかもしれないと 1 回知らせること。"""
        release = threading.Event()
        self.addCleanup(release.set)
        w, path = self._recording(release, limit=8.0)
        w.queue_output("rtrA", "line\r\n")
        _pump(300)                                  # 書き込みが詰まる
        w.queue_output("rtrA", "tail\r\n")          # 描く前に閉じる

        started = time.perf_counter()
        w.finish_log_recordings()
        took = time.perf_counter() - started

        self.assertLess(took, 7.0,
                        "詰まった記録を待って終了が %.1f 秒止まった" % took)
        self.assertGreaterEqual(took, 4.5,
                                "上限（5 秒）まで待たずに諦めた: %.1f 秒" % took)
        self.assertEqual(self.warning.call_count, 1,
                         "書き切れないまま閉じるのに知らせなかった（回数 %d）"
                         % self.warning.call_count)
        shown = " ".join(str(a) for a in self.warning.call_args[0])
        self.assertIn("記録が途中までの可能性があります", shown)
        self.assertIn("rtrA", shown)
        self.assertNotIn("rtrA", w._log_files)

        release.set()                               # 後から書き終わっても欠けない
        end = time.perf_counter() + 3.0
        while time.perf_counter() < end and self._read(path) != "line\ntail\n":
            _pump(20)
        self.assertEqual(self._read(path), "line\ntail\n")

    def test_closing_the_app_waits_only_until_the_log_is_written(self):
        """詰まりが上限より前に解ければ、書き終わるまで待ち、警告は出さないこと。"""
        from core import log_recording
        release = threading.Event()
        self.addCleanup(release.set)
        w, path = self._recording(release, limit=8.0)
        w.queue_output("rtrA", "line\r\n")
        _pump(300)                                  # 書き込みが詰まる
        w.queue_output("rtrA", "tail\r\n")
        threading.Timer(1.0, release.set).start()   # 1 秒後に応答が戻る

        started = time.perf_counter()
        w.finish_log_recordings()
        took = time.perf_counter() - started

        self.assertLess(took, 4.0, "書き終わったのに待ち続けた: %.1f 秒" % took)
        self.assertEqual(self.warning.call_count, 0,
                         "書き切れたのに警告が出た")
        self.assertEqual(self._read(path), "line\ntail\n",
                         "終了で閉じた記録が欠けた")
        self.assertIsNone(log_recording.device_using(path))


if __name__ == "__main__":
    unittest.main()
