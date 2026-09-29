"""記録先が詰まって止めた機器（関所あり）で、記録を停止すれば描画と受信が戻ることを検証する（4 周目 term の (2)(3)）。

何が起きていたか（b2858c4 で実測。scratchpad\\cx132e-term\\probe_hold.py。
上限 256 KiB / 64 KiB）: 記録待ちが上限を超えると、関所のある接続（SSH /
Telnet）はその機器の描画を止めて受信の関所を閉じる（termui-03 Q1 (b)）。
止めるかどうかの判定（_log_backlog）は、記録中のファイルに加えて、停止して
書き終えていない記録（_closing_logs）の書き込み待ちも数えていた。記録中
ダイアログの「記録停止」を押すと、停止より前に受信して描いていない分
（4,480 文字）ごと、詰まった書き込みスレッドが _closing_logs へ預けられる。
その書き込み待ち（264,960 文字）は記録先が戻るまで減らないので、停止しても
throttled=True・関所は閉じたまま・描き待ちは 4,480 文字のままで、画面は
L003839 から進まなかった。健全な別ファイルへ記録を始め直しても同じだった
（停止した記録の分を数え続ける）。解けたのは記録先が戻ったときだけ。

どう直したか（作り手の親の決定）: 止めるかどうかの判定では、停止した記録を
数えない（_held_log_backlog。記録中のファイルの分だけ）。停止した記録が
受け取るのは停止より前に受信した分だけで量に上限があるので、描いてその
書き込みスレッドへ積めばよい。記録と画面を欠かさない性質は保つ（停止した
記録には停止までの受信が全部、順序どおりに入り、停止後の受信は入らない）。

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


class LogRecordingIoStallStopReleasesHoldTest(unittest.TestCase):
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
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-stop-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        # 受信を止めたことの案内はモーダルでない（show() で出す）。exec の
        # モーダルへ戻る退行があっても、ここで止まったままにならないように、
        # exec はすぐ戻す（案内の出方は test_log_recording_io_stall_hold_notice*.py
        # で確かめる）
        mock.patch("PyQt6.QtWidgets.QMessageBox.exec", return_value=0).start()
        self.addCleanup(mock.patch.stopall)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        self.addCleanup(_close_notices, w)
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
        w.create_terminal_tab("dev")
        w.tab_widget.setCurrentWidget(w._terminals["dev"])
        return w

    def _start(self, w, name, stalled):
        path = os.path.join(self.dir, name)
        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if stalled and isinstance(file, str) and file == path and "w" in mode:
                return _StallingFile(f, self.release, 30.0)
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

    def _feed_until_held(self, w, gate):
        """受信スレッドの代わり: 関所が開いている間だけ受信を渡し、閉じたら止める。"""
        fed = 0
        end = time.perf_counter() + 4.0
        while gate.is_set() and time.perf_counter() < end:
            w.queue_output("dev", _chunk(fed, self.LINES))
            fed += self.LINES
            _pump(20)
        self.assertFalse(gate.is_set(), "前提: 記録待ちが上限を超えて受信を止めた")
        self.assertIn("dev", w._log_throttled, "前提: この機器の描画を止めている")
        self.assertTrue(w._pending_output.get("dev"),
                        "前提: 停止より前に受信して、まだ描いていない分がある")
        return fed

    def _resumed(self, w, gate, last):
        return (gate.is_set() and not w._pending_output.get("dev")
                and last in w._terminals["dev"].toPlainText())

    def test_stopping_the_stalled_recording_resumes_the_device(self):
        """止めている間に「記録停止」を押したら、記録先が詰まったままでも描画と受信が戻ること。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w, "dev.log", stalled=True)
        gate = w.output_gate("dev")
        fed = self._feed_until_held(w, gate)

        w._log_dialogs["dev"]._on_stop()        # 記録中ダイアログの「記録停止」
        self.assertNotIn("dev", w._log_files, "前提: 記録は止まった")
        last = "L%06d" % (fed - 1)
        self.assertTrue(
            self._wait_until(lambda: self._resumed(w, gate, last), 3.0),
            "記録を停止しても、止めた描画と受信が戻らない（throttled=%s、関所=%s、"
            "描き待ち %d 文字）" % ("dev" in w._log_throttled, gate.is_set(),
                                    len(w._pending_output.get("dev", ()))))
        self.assertFalse(self.release.is_set(), "前提: 記録先はまだ詰まっている")

        # 停止後の受信（打った文字のエコーなど）も、詰まったままのうちに描かれる
        w.queue_output("dev", "after-stop-echo\r\n")
        self.assertTrue(self._wait_until(
            lambda: "after-stop-echo" in w._terminals["dev"].toPlainText(), 2.0),
            "記録を停止したあとの受信が描かれない")

        # 停止した記録には、停止までの受信が全部、順序どおりに入る（停止後の分は入らない）
        self.release.set()
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0),
            "詰まりが解けても停止した記録が閉じない")
        with open(path, encoding="utf-8") as f:
            logged = f.read()
        self.assertEqual(logged, _chunk(0, fed).replace("\r\n", "\n"),
                         "停止より前に受信した分の記録が欠けた・崩れた・停止後の分が入った")
        self.assertEqual(self.warning.call_count, 0)

    def test_a_healthy_recording_started_after_the_stop_resumes_the_device(self):
        """停止してから健全な別ファイルへ記録を始め直したら、描画と受信が戻り、両方の記録が欠けないこと。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w, "dev.log", stalled=True)
        gate = w.output_gate("dev")
        fed = self._feed_until_held(w, gate)

        w.stop_log_recording("dev")
        path2 = self._start(w, "dev2.log", stalled=False)
        last = "L%06d" % (fed - 1)
        self.assertTrue(
            self._wait_until(lambda: self._resumed(w, gate, last), 3.0),
            "健全なファイルへ記録を始め直しても、止めた描画と受信が戻らない"
            "（throttled=%s、関所=%s）" % ("dev" in w._log_throttled, gate.is_set()))
        self.assertFalse(self.release.is_set(), "前提: 前の記録先はまだ詰まっている")

        w.queue_output("dev", _chunk(fed, self.LINES))
        after = fed + self.LINES
        self.assertTrue(self._wait_until(
            lambda: "L%06d" % (after - 1) in w._terminals["dev"].toPlainText(), 2.0),
            "始め直したあとの受信が描かれない")
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path2) is None, 2.0))
        with open(path2, encoding="utf-8") as f:
            self.assertEqual(f.read(), _chunk(fed, self.LINES).replace("\r\n", "\n"),
                             "始め直した記録に、始める前の受信が入った・欠けた")

        self.release.set()
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), _chunk(0, fed).replace("\r\n", "\n"),
                             "停止した記録が欠けた・崩れた")
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
