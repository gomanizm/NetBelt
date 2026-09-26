"""受信の関所が無い接続（シリアル）では、記録先が詰まっても描画を止めず、一度だけ知らせることを検証する（Codex 11 回目 term-03）。

何が起きていたか（c2bb66a で実測。scratchpad\\cx132c-termlog\\repro_term03.py。
上限 256 KiB / 64 KiB）: 記録待ちが上限を超えたら、その機器の描画を止めて受信の
関所（output_gate）を閉じる作り（termui-03 Q1 (b)）は、関所を持たないシリアルにも
描画の停止だけを当てていた。シリアルの受信スレッドは関所を見ないので受信は
queue_output へ積まれ続け、描き待ち（_pending_output）が上限なく増えた（20ms
ごとに約 30 KB を渡すと、0.5 秒で 0.34 MB、3 秒で 4.06 MB と増え続け、画面は
L004607 で止まったまま。知らせも出なかった）。

決まった振る舞い（作り手の親の決定。関所の無い接続への Q1 の当てはめ）: 関所の
無い接続では描画を止めない（画面は進める）。記録待ちは上限を超えても積み続け、
記録を欠かさない（シリアルの速度では 115200bps で 1 時間に最大約 41 MB）。上限を
超えたときに一度だけ『記録先への書き込みが遅れています』の趣旨の知らせを、ほかの
記録の知らせと同じ警告で出す。関所のある接続（SSH / Telnet）の Q1 (b) は変えない
（test_log_recording_io_stall_backpressure.py ほか）。

どう直したか: _flush_pending_output が描画を止めるのは、関所を持つ機器だけにした。
記録待ちの知らせは見回り（_check_log_writers）が出す。「一度だけ」は上限を
超えるたびに一度とし、記録待ちが PENDING_LOW_WATER まで減ったら（関所のある
接続が受信を再開するのと同じ水位）、次に上限を超えたときにまた一度知らせる。

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


class LogRecordingIoStallNoGateTest(unittest.TestCase):
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
        self.addCleanup(log_recording.stop, "ser")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-nogate-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
        # シリアルの接続には関所を渡さない（MainWindow._attach_read_gate）
        w.create_terminal_tab("ser")
        w.tab_widget.setCurrentWidget(w._terminals["ser"])
        return w

    def _start(self, w, release, limit):
        path = os.path.join(self.dir, "ser.log")
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
        self.assertIn("ser", w._log_files, "前提: 記録が始まっている")
        return path

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def _feed(self, w, first, chunks):
        """シリアルの受信スレッドの代わり: 関所を見ずに受信を渡し続ける。"""
        for i in range(chunks):
            w.queue_output("ser", _chunk(first + i * self.LINES, self.LINES))
            _pump(20)
        return first + chunks * self.LINES

    def _lag_notices(self):
        return [c for c in self.warning.call_args_list
                if len(c.args) > 2 and "遅れて" in c.args[2]]

    def test_a_stalled_log_on_a_link_without_a_gate_keeps_drawing_and_warns_once(self):
        """関所の無い接続では、記録先が詰まっても描き進め、記録を欠かさず、一度だけ知らせること。"""
        from core import log_recording
        w = self._widget()
        release = threading.Event()
        self.addCleanup(release.set)
        path = self._start(w, release, limit=30.0)
        self.assertNotIn("ser", w._output_gates, "前提: この接続には関所が無い")

        fed = self._feed(w, 0, 18)
        self.assertGreater(fed * 60, 2 * self.HIGH, "前提: 上限を十分に超える量を受信した")
        last = "L%06d" % (fed - 1)
        drawn = self._wait_until(
            lambda: not w._pending_output.get("ser")
            and last in w._terminals["ser"].toPlainText(), 10.0)
        self.assertTrue(drawn,
                        "記録先が詰まっている間、関所の無い接続の描画を止めた"
                        "（受信は止まらないので、描き待ち %d 文字が上限なく増える）"
                        % len(w._pending_output.get("ser", ())))
        self.assertNotIn("ser", w._log_throttled)
        self.assertFalse(release.is_set(), "前提: 記録先はまだ詰まっている")
        self.assertGreater(w._log_backlog("ser"), self.HIGH,
                           "上限を超えた記録待ちを捨てた（記録が欠ける）")
        self.assertTrue(self._wait_until(lambda: self._lag_notices(), 2.0),
                        "記録待ちが上限を超えたのに知らせなかった")
        notice = self._lag_notices()[0]
        self.assertEqual(notice.args[1], "ログ記録")
        self.assertIn("ser", notice.args[2])

        # 詰まったまま受信が続いても、知らせは一度だけ
        fed = self._feed(w, fed, 6)
        _pump(300)
        self.assertEqual(len(self._lag_notices()), 1, "知らせを繰り返した")
        self.assertEqual(self.warning.call_count, 1, "ほかの警告が出た")

        release.set()
        self.assertTrue(self._wait_until(
            lambda: not w._pending_output.get("ser")
            and w._log_backlog("ser") == 0, 10.0),
            "詰まりが解けても記録待ちが書き終わらない")
        w.stop_log_recording("ser")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0))
        with open(path, encoding="utf-8") as f:
            logged = f.read()
        self.assertEqual(logged, _chunk(0, fed).replace("\r\n", "\n"),
                         "詰まっている間の記録が欠けた・崩れた")
        self.assertIn("L%06d" % (fed - 1), w._terminals["ser"].toPlainText())
        self.assertEqual(self.warning.call_count, 1)

    def test_a_new_stall_after_catching_up_warns_once_more(self):
        """書き込み待ちが追いついたあと、もう一度詰まって上限を超えたら、また一度だけ知らせること。"""
        w = self._widget()
        release = threading.Event()
        self.addCleanup(release.set)
        self._start(w, release, limit=30.0)

        fed = self._feed(w, 0, 12)
        self.assertTrue(self._wait_until(lambda: self._lag_notices(), 2.0),
                        "前提: 記録待ちが上限を超えて知らせた")
        release.set()
        self.assertTrue(self._wait_until(
            lambda: not w._pending_output.get("ser")
            and w._log_backlog("ser") == 0, 10.0), "前提: 追いついた")
        _pump(300)
        self.assertEqual(len(self._lag_notices()), 1, "追いつくまでに知らせを繰り返した")

        release.clear()                 # 記録先がまた応答しなくなる
        self._feed(w, fed, 12)
        self.assertTrue(self._wait_until(
            lambda: len(self._lag_notices()) == 2, 2.0),
            "追いついたあとにまた上限を超えたのに、知らせなかった")
        _pump(300)
        self.assertEqual(len(self._lag_notices()), 2, "知らせを繰り返した")

    def test_a_link_with_a_gate_is_still_held_back_without_the_notice(self):
        """関所のある接続は、これまでどおり描画と受信を止め、遅れの知らせは出さないこと。"""
        w = self._widget()
        release = threading.Event()
        self.addCleanup(release.set)
        self._start(w, release, limit=30.0)
        gate = w.output_gate("ser")     # SSH / Telnet と同じく関所を渡す

        fed = 0
        end = time.perf_counter() + 4.0
        while gate.is_set() and time.perf_counter() < end:
            w.queue_output("ser", _chunk(fed, self.LINES))
            fed += self.LINES
            _pump(20)
        self.assertFalse(gate.is_set(), "記録待ちが上限を超えても受信を止めなかった")
        self.assertIn("ser", w._log_throttled)
        _pump(300)
        self.assertEqual(self._lag_notices(), [],
                         "受信を止められる接続にも遅れの知らせを出した")
        release.set()
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and not w._pending_output.get("ser"), 10.0),
            "詰まりが解けても受信・描画を再開しなかった")
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
