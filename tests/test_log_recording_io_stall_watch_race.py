"""見回りが失敗を読んだ直後に書き込みスレッドが失敗しても、次の受信を待たずに知らせることを検証する（termui-03）。

何が起きていたか（881ca05 で実測。scratchpad\\cx132-termlog-check-resume\\
my_race.py）: 見回り（TerminalWidget._check_log_writers）は、スレッドの失敗を
先頭で読み、見回りを止めるかは最後に「手が空いたか（busy）」だけで決めていた。
その間に、詰まっていた書き込みが失敗して手を空けると、失敗は残ったまま
見回りが止まる。2 秒たっても警告は出ず記録中のままで、知らせは次の受信か
停止まで遅れた（利用者は記録できていると思って作業を続ける）。

どう直したか: 止める前に、手が空いたのを見てから記録中の失敗を読み直し、
あれば止めない（スレッドは失敗を残してから手を空けるので、手が空いたのを
見たあとなら失敗も見える）。

スレッドの順番は、見回りが失敗を読んだ（まだ無かった）直後に詰まりを解いて、
スレッドが失敗して手を空けるまで待つ形で決め打ちする。
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
    ならないように）。放されたあとの write は fail の例外で失敗する。
    """

    def __init__(self, f, release, limit, fail):
        self._f = f
        self.name = f.name
        self._release = release
        self._limit = limit
        self._fail = fail

    def write(self, text):
        self._release.wait(self._limit)
        raise self._fail

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


class LogRecordingIoStallWatchRaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-race-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def test_a_failure_right_after_the_watch_looked_is_still_reported(self):
        """見回りが失敗を読んだ直後にスレッドが失敗して手を空けても、次の受信を待たずに知らせること。"""
        from core.log_writer import LogWriter
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        release = threading.Event()
        self.addCleanup(release.set)
        path = os.path.join(self.dir, "rtrA.log")
        real_open = builtins.open
        error = OSError(64, "The specified network name is no longer available")

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if isinstance(file, str) and file == path and "w" in mode:
                return _StallingFile(f, release, 10.0, error)
            return f

        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        handle = w._log_files.get("rtrA")
        self.assertIsInstance(handle, LogWriter, "前提: 記録が始まっている")

        w.queue_output("rtrA", "line\r\n")
        self.assertTrue(self._wait_until(
            lambda: handle.busy and w._log_watch.isActive(), 2.0),
            "前提: 記録先が詰まり、見回りが動いている")

        # 見回りが失敗を読んで「まだ無い」と見た直後に、詰まりが解けて
        # 書き込みが失敗し、スレッドが手を空ける
        real_failure = LogWriter.failure
        fired = []

        def failure(writer):
            value = real_failure.fget(writer)
            if value is None and writer is handle and not fired:
                fired.append(True)
                release.set()
                writer.wait(3.0)
            return value
        mock.patch.object(LogWriter, "failure", property(failure)).start()
        self.assertTrue(self._wait_until(lambda: bool(fired), 2.0),
                        "前提: 見回りが失敗を読んだ")
        self.assertIs(real_failure.fget(handle), error, "前提: 書き込みが失敗した")
        self.assertFalse(handle.busy, "前提: スレッドが手を空けた")

        # 次の受信は来ない
        self.assertTrue(
            self._wait_until(lambda: self.warning.call_count > 0, 1.5),
            "見回りが失敗を読んだ直後に書き込みが失敗すると、次の受信か停止まで"
            "知らせなかった（利用者は記録中だと思い続ける）")
        self.assertNotIn("rtrA", w._log_files, "失敗した記録が記録中のまま残った")
        _pump(300)
        self.assertEqual(self.warning.call_count, 1, "失敗の警告が 1 回ではない")


if __name__ == "__main__":
    unittest.main()
