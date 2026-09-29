"""記録先が詰まって記録待ちが上限を超えたら、その機器だけ受信を待たせることを検証する（termui-03 Q1）。

何が起きていたか（441ea02 で実測。scratchpad\\cx132-termlog\\repro\\
repro_termui03_io_stall.py）: 記録の書き込みは GUI スレッドで同期に走って
いたので、記録先が応答しないと GUI 全体が止まっていた。書き込みを専用の
スレッドへ移すと、今度は詰まっている間の記録待ちがメモリに積み上がり続ける。

利用者の決定（Q1 (b)）: 記録待ちが上限（描き待ちと同じ PENDING_HIGH_WATER、
8 MiB）を超えたら、その機器だけ描くのを止めて受信の関所（output_gate）を閉じる。
機器側が待つので、記録も画面も欠けない。ほかのタブは動く。記録待ちが
PENDING_LOW_WATER まで減ったら再開する。

どう直したか: _flush_pending_output が機器ごとの記録待ち（記録中のファイルの
書き込みスレッドに積んだ文字数。_held_log_backlog）を見て、上限を超えて
いればその機器を描かずに関所を閉じる。見回り（_check_log_writers）が、減ったら
関所を開け直して描画を再開させる。停止して書き終えていない記録の分は数えない
（数えると、記録を停止しても止めたままになった。4 周目 term。
test_log_recording_io_stall_stop_releases_hold.py）。止めたことは、見回りが
一度だけ案内する（警告ではない。test_log_recording_io_stall_hold_notice.py）。

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


def _chunk(first, count):
    return "".join("L%06d %s\r\n" % (i, "y" * 50) for i in range(first, first + count))


def _close_notices(owner):
    """owner を親にして開いたままの案内（QMessageBox）を閉じる（後のテストへ残さない）。"""
    from PyQt6.QtWidgets import QMessageBox
    for box in owner.findChildren(QMessageBox):
        box.close()


class LogRecordingIoStallBackpressureTest(unittest.TestCase):
    HIGH = 256 * 1024
    LOW = 64 * 1024
    LINES = 512            # 1 回に受信させる行数（約 30 KB）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-bp-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        # 受信を止めたことの案内はモーダルでない（show() で出す）。exec の
        # モーダルへ戻る退行があっても、ここで止まったままにならないように、
        # exec はすぐ戻す（案内の出方は test_log_recording_io_stall_hold_notice*.py
        # で確かめる）
        mock.patch("PyQt6.QtWidgets.QMessageBox.exec", return_value=0).start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        self.addCleanup(_close_notices, w)
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
        w.create_terminal_tab("other")
        w.create_terminal_tab("dev")
        w.tab_widget.setCurrentWidget(w._terminals["dev"])
        return w

    def _start(self, w, release, limit):
        path = os.path.join(self.dir, "dev.log")
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
        self.assertIn("dev", w._log_files, "前提: 記録が始まっている")
        return path

    @staticmethod
    def _wait_until(predicate, seconds=5.0):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def test_a_stalled_log_holds_back_only_its_own_device(self):
        """記録待ちが上限を超えたら、その機器の描画と受信を止め、減ったら欠けなく再開すること。"""
        from core import log_recording
        w = self._widget()
        release = threading.Event()
        self.addCleanup(release.set)
        path = self._start(w, release, limit=1.0)
        gate = w.output_gate("dev")

        # 受信スレッドの代わり: 関所が開いている間だけ受信を渡す
        fed = 0
        end = time.perf_counter() + 4.0
        while gate.is_set() and time.perf_counter() < end:
            w.queue_output("dev", _chunk(fed, self.LINES))
            fed += self.LINES
            _pump(20)

        self.assertFalse(gate.is_set(),
                         "記録待ちが上限を超えても受信を止めなかった"
                         "（詰まっている間、記録待ちがメモリに積み上がり続ける）")
        self.assertGreater(fed * 60, self.HIGH, "前提: 上限を超える量を受信した")
        waiting = len(w._pending_output.get("dev", ()))
        shown = w._terminals["dev"].toPlainText()
        _pump(300)
        self.assertEqual(len(w._pending_output.get("dev", ())), waiting,
                         "記録待ちが上限を超えているのに描き進めた")
        self.assertEqual(w._terminals["dev"].toPlainText(), shown,
                         "記録待ちが上限を超えているのに描き進めた")
        self.assertFalse(gate.is_set(), "止めた受信を、減る前に再開した")

        # ほかのタブは動く
        w.queue_output("other", "other-alive\r\n")
        self.assertTrue(self._wait_until(
            lambda: "other-alive" in w._terminals["other"].toPlainText(), 1.0),
            "詰まった記録と関係の無いタブまで止まった")

        release.set()
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and not w._pending_output.get("dev")),
            "詰まりが解けても受信・描画を再開しなかった")
        last = "L%06d" % (fed - 1)
        self.assertIn(last, w._terminals["dev"].toPlainText(),
                      "再開後に画面が最後まで描かれない")

        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None))
        with open(path, encoding="utf-8") as f:
            logged = f.read()
        self.assertEqual(logged, _chunk(0, fed).replace("\r\n", "\n"),
                         "受信を待たせている間の記録が欠けた・崩れた")
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
