"""記録先の詰まりで止めた機器が、ほかのタブが受信し続けていても再開し、失敗も知らせることを検証する（termui-03 Q1）。

何が起きていたか（3b2fcbe で実測。scratchpad\\cx132-termlog-check-resume\\
my_watch_restart.py）: 記録待ちが上限を超えて止めた機器は、見回り
（TerminalWidget._check_log_writers、100ms ごと）が詰まりの解けたのを見て
再開させ、記録の失敗もそこで知らせる。ところが _flush_pending_output は、
止めている機器を通るたびに見回りのタイマーを start() し直していた。
QTimer.start() は動いているタイマーを最初から数え直すので、ほかのタブが
100ms より短い間隔で受信し続けると、見回りは一度も動かない。30ms ごとに
受信させると、詰まりが解けて記録待ちが 0 になっても 3 秒以上関所が閉じた
ままで、書き込みの失敗も 3 秒間知らせなかった（ほかのタブの受信を止めると
0.1 秒で再開・警告した）。

利用者の決定（Q1 (b)）: 記録待ちが減ったら再開する。記録の失敗は次の受信を
待たずに知らせる（知らせないと、利用者は記録できていると思って作業を続ける）。

どう直したか: 見回りは、動いていないときだけ start する（_start_log_watch）。

このテストでは上限をインスタンスで小さくして（256 KiB / 64 KiB）確かめる。
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


def _chunk(first, count):
    return "".join("L%06d %s\r\n" % (i, "y" * 50) for i in range(first, first + count))


class LogRecordingIoStallWatchTest(unittest.TestCase):
    HIGH = 256 * 1024
    LOW = 64 * 1024
    LINES = 512            # 1 回に受信させる行数（約 30 KB）
    OTHER_INTERVAL = 20    # ほかのタブへ受信させる間隔（ms。見回りの 100ms より短い）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-watch-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
        w.create_terminal_tab("other")
        w.create_terminal_tab("dev")
        w.tab_widget.setCurrentWidget(w._terminals["dev"])
        return w

    def _start(self, w, release, limit, fail=None):
        """記録先だけを詰まるファイルにして記録を始め、パスを返す。"""
        path = os.path.join(self.dir, "dev.log")
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
        self.assertIn("dev", w._log_files, "前提: 記録が始まっている")
        return path

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def _hold_back_dev(self, fail=None):
        """dev の記録を詰まらせ、記録待ちが上限を超えて受信を止めるまで受信させる。

        そのあと、ほかのタブへ OTHER_INTERVAL ごとに受信させ続ける。
        （ウィジェット, 詰まりを解く合図, dev の関所, dev へ受信させた行数）を返す。
        """
        from PyQt6.QtCore import QTimer
        w = self._widget()
        release = threading.Event()
        self.addCleanup(release.set)
        self._start(w, release, limit=10.0, fail=fail)
        gate = w.output_gate("dev")

        # 受信スレッドの代わり: 関所が開いている間だけ受信を渡す
        fed = 0
        end = time.perf_counter() + 4.0
        while gate.is_set() and time.perf_counter() < end:
            w.queue_output("dev", _chunk(fed, self.LINES))
            fed += self.LINES
            _pump(20)
        self.assertFalse(gate.is_set(), "前提: 記録待ちが上限を超えて受信を止めた")

        # ほかのタブ（別の機器で show tech などを流し続けている）
        count = [0]

        def feed_other():
            w.queue_output("other", "o%05d\r\n" % count[0])
            count[0] += 1
        other = QTimer()
        other.setInterval(self.OTHER_INTERVAL)
        other.timeout.connect(feed_other)
        other.start()
        self.addCleanup(other.stop)
        _pump(300)
        self.assertFalse(gate.is_set(), "前提: 詰まっている間は止めたまま")
        self.other = other
        return w, release, gate, fed

    def test_a_held_back_device_resumes_while_another_tab_keeps_receiving(self):
        """ほかのタブが受信し続けていても、詰まりが解けたら止めた機器の受信・描画を再開すること。"""
        w, release, gate, fed = self._hold_back_dev()

        release.set()
        resumed = self._wait_until(
            lambda: gate.is_set() and not w._pending_output.get("dev"), 3.0)
        self.assertTrue(self.other.isActive(), "前提: ほかのタブは受信し続けている")
        self.assertTrue(resumed,
                        "ほかのタブが受信している間、詰まりが解けても受信・描画を"
                        "再開しなかった（記録待ち %d 文字）" % w._log_backlog("dev"))
        self.assertIn("L%06d" % (fed - 1), w._terminals["dev"].toPlainText(),
                      "再開後に画面が最後まで描かれない")
        self.assertEqual(self.warning.call_count, 0)

    def test_a_failure_while_held_back_is_reported_while_another_tab_keeps_receiving(self):
        """ほかのタブが受信し続けていても、止めている機器の記録の失敗を次の受信を待たずに知らせること。"""
        w, release, gate, _ = self._hold_back_dev(
            fail=OSError(64, "The specified network name is no longer available"))

        release.set()
        reported = self._wait_until(lambda: self.warning.call_count > 0, 3.0)
        self.assertTrue(self.other.isActive(), "前提: ほかのタブは受信し続けている")
        self.assertTrue(reported,
                        "ほかのタブが受信している間、止めている機器の記録の失敗を"
                        "知らせなかった（利用者は記録中だと思い続ける）")
        self.assertNotIn("dev", w._log_files, "失敗した記録が記録中のまま残った")
        _pump(300)
        self.assertEqual(self.warning.call_count, 1, "失敗の警告が 1 回ではない")
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and not w._pending_output.get("dev"), 3.0),
            "記録が止まったあとも受信・描画を再開しなかった")


if __name__ == "__main__":
    unittest.main()
