"""記録を閉じた直後に詰まりが解けて書き込みが失敗しても、その失敗を知らせることを検証する（termui-03）。

何が起きていたか（881ca05 で実測。scratchpad\\cx132-check-termlog\\
chk_close_race.py）: TerminalWidget._close_log_file は、先に close() を呼び、
そのあとで「手が空いたか（busy）」を見ていた。close() は記録先が詰まっていると
待たずに戻り、その時点の失敗しか見ない。その直後、busy を読む前に詰まりが
解けて書き込みが失敗し、スレッドが閉じ終えて手を空けると、busy は False に
なって登録を外すだけで戻った。失敗は LogWriter.failure に残っているのに、
知らせる経路がどこにも無い（停止・停止した記録の区間を描き切ったとき・
タブを閉じる、の 3 経路とも警告 0 回で、記録は黙って欠けた）。

どう直したか: 手が空いていた側でも、busy を見たあとに失敗を読み直す
（スレッドは失敗を残してから手を空けるので、手が空いたのを見たあとなら
失敗も見える）。

スレッドの順番は、閉じたあとの busy を読む瞬間に詰まりを解いて、スレッドが
失敗して手を空けるまで待つ形で決め打ちする
（test_log_recording_io_stall_watch_race.py と同じ作り）。
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


class LogRecordingIoStallCloseRaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-close-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def _stalled_recording(self):
        """rtrA の記録先を詰まらせ、閉じたあとの busy を読む瞬間に失敗させる。

        戻り値: (TerminalWidget, 記録先のパス, 失敗させたか)
        """
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

        w.queue_output("rtrA", "one\r\n")
        self.assertTrue(self._wait_until(
            lambda: handle.busy and handle._stalled, 2.0),
            "前提: 記録先が詰まっている")

        # 閉じたあとで busy を読む瞬間に、詰まりが解けて書き込みが失敗し、
        # スレッドが閉じ終えて手を空ける
        real_busy = LogWriter.busy
        fired = []

        def busy(writer):
            if writer is handle and writer._closing and not fired:
                fired.append(True)
                release.set()
                writer.wait(3.0)
            return real_busy.fget(writer)
        mock.patch.object(LogWriter, "busy", property(busy)).start()
        return w, path, fired

    def _assert_reported_once(self, path, fired, text):
        from core import log_recording
        self.assertTrue(self._wait_until(lambda: bool(fired), 2.0),
                        "前提: 閉じたあとの busy を読んだ")
        self.assertTrue(
            self._wait_until(lambda: self.warning.call_count > 0, 1.5),
            "閉じた直後に詰まりが解けて書き込みが失敗すると、その失敗を知らせず"
            "に登録を外した（記録が黙って欠ける）")
        _pump(300)
        self.assertEqual(self.warning.call_count, 1, "失敗の警告が 1 回ではない")
        self.assertIn(text, self.warning.call_args.args[2])
        self.assertIsNone(log_recording.device_using(path),
                          "閉じ終えた記録の使用中の登録が残った")

    def test_stopping_reports_a_failure_right_after_close(self):
        """停止した直後に書き込みが失敗しても、知らせること。"""
        w, path, fired = self._stalled_recording()
        w.stop_log_recording("rtrA")
        self.assertNotIn("rtrA", w._log_files)
        self._assert_reported_once(
            path, fired, "ログファイルを閉じる際にエラーが発生しました")

    def test_closing_a_stopped_log_reports_a_failure_right_after_close(self):
        """停止した記録の区間を描き切って閉じた直後に失敗しても、知らせること。"""
        w, path, fired = self._stalled_recording()
        # 描く前に止める（停止より前に受信した分が、停止した記録の区間に残る）
        w.queue_output("rtrA", "two\r\n")
        w.stop_log_recording("rtrA")
        self.assertIn("rtrA", w._closing_logs, "前提: 停止した記録の区間が残っている")
        self._assert_reported_once(
            path, fired, "停止より前に受信した分を書き終えられませんでした")

    def test_closing_the_tab_reports_a_failure_right_after_close(self):
        """タブを閉じた直後に書き込みが失敗しても、知らせること。"""
        w, path, fired = self._stalled_recording()
        w._close_tab(w.tab_widget.indexOf(w._terminals["rtrA"]))
        self.assertNotIn("rtrA", w._log_files)
        self._assert_reported_once(
            path, fired, "ログファイルを閉じる際にエラーが発生しました")


if __name__ == "__main__":
    unittest.main()
