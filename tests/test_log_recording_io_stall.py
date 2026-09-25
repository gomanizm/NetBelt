"""記録先の I/O が応答しなくなっても、GUI が止まらないことを検証する（termui-03）。

何が起きていたか（441ea02 で実測。scratchpad\\cx132-termlog\\repro\\
repro_termui03_io_stall.py）: 端末のログ記録は、描いた受信を GUI スレッドで
そのまま write / flush し、停止では close していた（TerminalWidget._write_logs /
_stop_log_recording_for）。記録先が共有フォルダや USB で応答しないと、その間
イベントループが回らない。書き込みが 1 秒ずつ止まる代役に替えると、10ms の
QTimer の最長間隔は受信 1 片で 2.004 秒（write と flush）、停止で 1.002 秒
（close）になった。全タブの描画・打鍵・停止ボタンが効かない。止まる長さは
SMB の時間切れしだいで数十秒になりうる。

どう直したか: 記録ごとに書き込みスレッドを置く（core/log_writer.py の
LogWriter）。GUI は書き終わりを短い間だけ待ち、書き込みの呼び出しが戻らなければ
「詰まっている」とみなして、以後は積むだけにする（普段のローカルディスクでは
待ちの中で書き終わるので、これまでと同じく「呼んだら書けている」）。スレッドで
起きた失敗は、次の受信を待たずに見回り（TerminalWidget._check_log_writers）が
知らせる。停止した記録の使用中の登録は、スレッドが閉じ終えてから外す（先に
外すと、同じファイルへの次の記録が前の記録の書き込みと混ざる）。記録中ダイアログの
バイト数は、実際に書けた分だけを数える。

詰まりは、記録を始めるときの open を差し替えて「放されるまで write / flush /
close が戻らない」ファイルを渡して作る（作りに依らない形）。
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
    ならないように）。fail を渡すと、放されたあとの write がその例外で失敗する。
    """

    def __init__(self, f, release, limit, fail=None):
        self._f = f
        self.name = f.name
        self._release = release
        self._limit = limit
        self._fail = fail

    def write(self, text):
        self._release.wait(self._limit)
        if self._fail is not None:
            raise self._fail
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


class LogRecordingIoStallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        return w

    def _release(self):
        """詰まりを解く合図。後始末で必ず解く（書き込みスレッドを残さない）。"""
        release = threading.Event()
        self.addCleanup(release.set)
        return release

    def _start(self, w, release, limit, fail=None):
        """記録先だけを詰まるファイルにして記録を始め、パスを返す。"""
        path = os.path.join(self.dir, "rtrA.log")
        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if isinstance(file, str) and file == path and "w" in mode:
                return _StallingFile(f, release, limit, fail)
            return f

        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn("rtrA", w._log_files, "前提: 記録が始まっている")
        return path

    @staticmethod
    def _max_gap(action, ms):
        """action を呼んで ms ミリ秒回す間の、10ms タイマーの刻みの最長間隔（秒）。"""
        from PyQt6.QtCore import QTimer
        ticks = [time.perf_counter()]
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
        timer.start()
        action()
        _pump(ms)
        timer.stop()
        ticks.append(time.perf_counter())
        return max(b - a for a, b in zip(ticks, ticks[1:]))

    @staticmethod
    def _wait_until(predicate, seconds=3.0):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_a_stalled_write_does_not_stop_the_event_loop(self):
        """書き込みが戻らない間もイベントループが回り、放したあと記録に欠けが無いこと。"""
        w = self._widget()
        release = self._release()
        path = self._start(w, release, limit=2.0)

        gap = self._max_gap(lambda: w.queue_output("rtrA", "line\r\n"), 600)

        self.assertLess(gap, 0.3,
                        "記録先の書き込み待ちで GUI が %.2f 秒止まった" % gap)
        self.assertIn("line", w._terminals["rtrA"].toPlainText(),
                      "記録先が詰まっている間に画面まで止まった")
        self.assertEqual(w.recorded_bytes("rtrA"), 0,
                         "まだ書けていない分を「記録したバイト数」に数えた")

        release.set()
        self.assertTrue(
            self._wait_until(lambda: w.recorded_bytes("rtrA")
                             == len("line" + os.linesep)),
            "詰まりが解けても書けた分が数えられない: %d" % w.recorded_bytes("rtrA"))
        w.stop_log_recording("rtrA")
        from core import log_recording
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None))
        self.assertEqual(self._read(path), "line\n",
                         "止まっていた間の受信が記録から欠けた")

    def test_stopping_during_a_stall_returns_at_once(self):
        """詰まっている最中の停止がすぐ戻り、閉じ終えるまで使用中の登録が残ること。"""
        from core import log_recording
        w = self._widget()
        release = self._release()
        path = self._start(w, release, limit=1.5)
        w.queue_output("rtrA", "line\r\n")
        _pump(300)                                  # 書き込みが詰まる

        started = time.perf_counter()
        w.stop_log_recording("rtrA")
        took = time.perf_counter() - started

        self.assertLess(took, 0.1,
                        "詰まっている記録の停止で GUI が %.2f 秒止まった" % took)
        self.assertNotIn("rtrA", w._log_files, "停止したのに記録中のまま")
        self.assertNotIn("rtrA", w._log_dialogs, "記録中ダイアログが残った")
        self.assertFalse(w._terminals["rtrA"]._is_recording)
        self.assertEqual(log_recording.device_using(path), "rtrA",
                         "閉じ終える前に使用中の登録を外した（同じファイルへの"
                         "次の記録が、前の記録の書き込みと混ざる）")

        release.set()
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None),
            "閉じ終えても使用中の登録が残った")
        self.assertEqual(self._read(path), "line\n")
        self.assertEqual(self.warning.call_count, 0,
                         "閉じるのに成功したのに警告が出た")

    def test_a_failure_after_the_stall_is_reported_without_more_output(self):
        """詰まったあとに書き込みが失敗したら、次の受信を待たずに 1 回だけ知らせること。"""
        from core import log_recording
        w = self._widget()
        release = self._release()
        path = self._start(
            w, release, limit=1.5,
            fail=OSError(64, "The specified network name is no longer available"))

        gap = self._max_gap(lambda: w.queue_output("rtrA", "line\r\n"), 300)
        self.assertLess(gap, 0.3,
                        "記録先の書き込み待ちで GUI が %.2f 秒止まった" % gap)
        self.assertEqual(self.warning.call_count, 0, "前提: まだ失敗していない")

        release.set()
        self.assertTrue(self._wait_until(lambda: self.warning.call_count > 0),
                        "書き込みスレッドの失敗を知らせなかった（記録が黙って欠ける）")
        _pump(300)
        self.assertEqual(self.warning.call_count, 1,
                         "警告の回数が違う: %d" % self.warning.call_count)
        shown = " ".join(str(a) for a in self.warning.call_args[0])
        self.assertIn("rtrA", shown)
        self.assertIn("network name is no longer available", shown)
        self.assertNotIn("rtrA", w._log_files, "失敗したのに記録中のまま")
        self.assertFalse(w._terminals["rtrA"]._is_recording)
        self.assertNotIn("rtrA", w._log_dialogs, "記録中ダイアログが残った")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None),
            "失敗した記録の使用中の登録が残った")

    def test_closing_a_tab_during_a_stall_keeps_the_undrawn_output(self):
        """詰まっている最中にタブを閉じてもすぐ戻り、描いていなかった受信も記録に入ること。"""
        from core import log_recording
        w = self._widget()
        release = self._release()
        path = self._start(w, release, limit=1.5)
        w.queue_output("rtrA", "one\r\n")
        _pump(300)                                  # 書き込みが詰まる
        w.queue_output("rtrA", "two\r\n")           # 描く前に閉じる

        started = time.perf_counter()
        w._close_tab(w.tab_widget.indexOf(w._terminals["rtrA"]))
        took = time.perf_counter() - started

        self.assertLess(took, 0.3,
                        "詰まっている記録のタブを閉じるのに %.2f 秒止まった" % took)
        release.set()
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None),
            "閉じ終えても使用中の登録が残った")
        self.assertEqual(self._read(path), "one\ntwo\n",
                         "閉じたタブの描いていなかった受信が記録から欠けた")


if __name__ == "__main__":
    unittest.main()
