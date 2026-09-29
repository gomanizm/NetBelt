"""記録先が詰まって止めている機器の分しか残っていないとき、描画タイマーが空回りしないことを検証する（termlog 見張りの穴 2）。

何が起きていたか（ebbe593 で実測。scratchpad\\cx132b-termlog\\insp_spin.py）:
_flush_pending_output の finally は「止めている機器（_log_throttled）の分しか
描き待ちが残っていなければ、0ms の描画タイマーを掛け直さない」。この条件を
守るテストが無く、元の `if self._pending_output:` に戻しても
test_log_recording_io_stall*.py と test_log_recording_keeps_queued_output.py の
21 件が全部通った。戻すと、記録先が詰まっている間ずっと、描けない機器のために
0ms タイマーが回り続ける（1 秒で _flush_pending_output が 99,197 回、
CPU 0.84 秒。直した作りでは 0 回）。

どうしたか: 本体は正しい。止めている機器の分しか残っていないとき、
_flush_pending_output がタイマーを掛け直さないことと、イベントループを回しても
呼ばれ続けないこと、詰まりが解ければ欠けなく描き終えることを確かめるテストを
足した。上の条件を元に戻すと、このテストが落ちる。

上限はインスタンスで小さくして（256 KiB / 64 KiB）確かめる
（test_log_recording_io_stall_backpressure.py と同じ）。
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

    放されないまま limit 秒たったら戻る（テストが止まったままにならないように）。
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


class LogRecordingIoStallNoSpinTest(unittest.TestCase):
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
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-spin-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        # 記録先の詰まりの知らせは窓を出さない（記録中ダイアログの状態の行。
        # test_log_recording_io_stall_status_line.py ほか）。窓を出す退行が
        # あっても、ここで止まったままにならないように、exec はすぐ戻す
        mock.patch("PyQt6.QtWidgets.QMessageBox.exec", return_value=0).start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self, calls):
        """描画タイマーが _flush_pending_output を呼んだ回数を calls へ数える端末を作る"""
        from ui.terminal_widget import TerminalWidget
        real = TerminalWidget._flush_pending_output

        def counting(widget):
            calls.append(1)
            return real(widget)

        # タイマーへつなぐのは __init__ の中なので、作る間だけ差し替えれば足りる
        with mock.patch.object(TerminalWidget, "_flush_pending_output", counting):
            w = TerminalWidget()
        self.addCleanup(w.close)
        self.addCleanup(_close_notices, w)
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
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

    def test_the_draw_timer_does_not_spin_for_a_held_back_device(self):
        """止めている機器の分しか残っていなければ、描画タイマーを掛け直さず空回りしないこと。"""
        from core import log_recording
        calls = []
        w = self._widget(calls)
        release = threading.Event()
        self.addCleanup(release.set)
        path = self._start(w, release, limit=5.0)
        gate = w.output_gate("dev")

        # 受信スレッドの代わり: 関所が開いている間だけ受信を渡す
        fed = 0
        end = time.perf_counter() + 4.0
        while gate.is_set() and time.perf_counter() < end:
            w.queue_output("dev", _chunk(fed, self.LINES))
            fed += self.LINES
            _pump(20)
        self.assertFalse(gate.is_set(), "前提: 記録待ちが上限を超えて受信を止めた")
        self.assertIn("dev", w._log_throttled, "前提: この機器の描画を止めている")
        self.assertTrue(w._pending_output.get("dev"),
                        "前提: 止めている機器の描き待ちが残っている")
        self.assertEqual(set(w._pending_output), {"dev"},
                         "前提: 描き待ちは止めている機器の分だけ")

        # 止まった描画タイマーから 1 回まわしたあと、止まったままであること
        w._output_timer.stop()
        w._flush_pending_output()
        self.assertFalse(w._output_timer.isActive(),
                         "止めている機器の分しか無いのに 0ms の描画タイマーを掛け直した")

        # イベントループを回しても呼ばれ続けないこと（空回りすると 1 秒で数万回）
        _pump(50)
        del calls[:]
        _pump(500)
        self.assertLess(len(calls), 20,
                        "記録先が詰まっている間、描けない機器のために描画タイマーが"
                        "空回りした（0.5 秒で %d 回）" % len(calls))
        self.assertFalse(gate.is_set(), "止めた受信を、減る前に再開した")

        # 詰まりが解ければ、欠けなく描き終えて受信も再開する
        release.set()
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and not w._pending_output.get("dev")),
            "詰まりが解けても受信・描画を再開しなかった")
        self.assertIn("L%06d" % (fed - 1), w._terminals["dev"].toPlainText(),
                      "再開後に画面が最後まで描かれない")

        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None))
        with open(path, encoding="utf-8") as f:
            logged = f.read()
        self.assertEqual(logged, _chunk(0, fed).replace("\r\n", "\n"),
                         "止めている間の記録が欠けた・崩れた")
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
