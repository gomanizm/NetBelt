"""受信の関所が無い接続（シリアル）では、記録先が詰まっても描画を止めず、上限を超えている間は記録中ダイアログの状態の行で知らせることを検証する（Codex 11 回目 term-03）。

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
超えたら『記録先への書き込みが遅れています』の趣旨を知らせる。関所のある接続
（SSH / Telnet）の Q1 (b)（描画と受信を止める）は変えない
（test_log_recording_io_stall_backpressure.py ほか）。関所のある接続にはこの知らせを
出さない。止めたことは、別の文面で知らせる（4 周目 term。
test_log_recording_io_stall_hold_notice.py）。

どう直したか: _flush_pending_output が描画を止めるのは、関所を持つ機器だけにした。
記録待ちの知らせは見回り（_check_log_writers）が出す。初めは上限を超えるたびに
一度、QMessageBox.warning（モーダル）で出していたが、出た瞬間にキーの行き先を
奪った（6 周目の検査役の指摘。test_log_recording_io_stall_status_line.py）。いまは
窓を出さず、記録中ダイアログの状態の行に、上限を超えてから記録待ちが
PENDING_LOW_WATER まで減る（関所のある接続が受信を再開するのと同じ水位）まで出す。
また上限を超えたら、また出す。

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
        # 記録先の詰まりの知らせは窓を出さない（記録中ダイアログの状態の行。
        # test_log_recording_io_stall_status_line.py ほか）。窓を出す退行が
        # あっても、ここで止まったままにならないように、exec はすぐ戻す
        mock.patch("PyQt6.QtWidgets.QMessageBox.exec", return_value=0).start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        self.addCleanup(_close_notices, w)
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
        """シリアル向けの遅れを警告（窓）で出した呼び出し"""
        return [c for c in self.warning.call_args_list
                if len(c.args) > 2 and "遅れて" in c.args[2]]

    @staticmethod
    def _line(w):
        """ser の記録中ダイアログの状態の行（表に出ていなければ ""。ダイアログか行が無ければ None）"""
        dialog = w._log_dialogs.get("ser")
        label = getattr(dialog, "status_label", None)
        if label is None:
            return None
        return label.text() if label.isVisibleTo(dialog) else ""

    def _lag_line(self, w):
        """いま出ているシリアル向けの遅れの行（出ていなければ None）"""
        line = self._line(w)
        return line if line and "メモリに溜めて" in line else None

    def test_a_stalled_log_on_a_link_without_a_gate_keeps_drawing_and_shows_the_lag(self):
        """関所の無い接続では、記録先が詰まっても描き進め、記録を欠かさず、上限を超えている間は状態の行で知らせること（窓は出さない）。"""
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
        self.assertTrue(self._wait_until(lambda: self._lag_line(w), 2.0),
                        "記録待ちが上限を超えたのに知らせなかった: %r" % self._line(w))
        self.assertEqual(w._log_dialogs["ser"].device_name, "ser")
        self.assertIn("記録先への書き込みが遅れています", self._lag_line(w))
        self.assertIn("保存先の接続を確認", self._lag_line(w))

        # 詰まったまま受信が続く間は出たまま。窓は出さない
        fed = self._feed(w, fed, 6)
        _pump(300)
        self.assertTrue(self._lag_line(w), "詰まったままなのに状態の行を消した")
        self.assertEqual(self._lag_notices(), [], "遅れを警告の窓で出した")
        self.assertEqual(self.warning.call_count, 0, "警告が出た")

        release.set()
        self.assertTrue(self._wait_until(
            lambda: not w._pending_output.get("ser")
            and w._log_backlog("ser") == 0, 10.0),
            "詰まりが解けても記録待ちが書き終わらない")
        self.assertTrue(self._wait_until(lambda: self._line(w) == "", 2.0),
                        "追いついたのに状態の行を消さなかった: %r" % self._line(w))
        w.stop_log_recording("ser")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0))
        with open(path, encoding="utf-8") as f:
            logged = f.read()
        self.assertEqual(logged, _chunk(0, fed).replace("\r\n", "\n"),
                         "詰まっている間の記録が欠けた・崩れた")
        self.assertIn("L%06d" % (fed - 1), w._terminals["ser"].toPlainText())
        self.assertEqual(self.warning.call_count, 0)

    def test_a_new_stall_after_catching_up_shows_the_lag_again(self):
        """書き込み待ちが追いついたら状態の行を消し、もう一度詰まって上限を超えたら、また出すこと（窓は出さない）。"""
        w = self._widget()
        release = threading.Event()
        self.addCleanup(release.set)
        self._start(w, release, limit=30.0)

        fed = self._feed(w, 0, 12)
        self.assertTrue(self._wait_until(lambda: self._lag_line(w), 2.0),
                        "前提: 記録待ちが上限を超えて知らせた")
        release.set()
        self.assertTrue(self._wait_until(
            lambda: not w._pending_output.get("ser")
            and w._log_backlog("ser") == 0, 10.0), "前提: 追いついた")
        self.assertTrue(self._wait_until(lambda: self._line(w) == "", 2.0),
                        "追いついたのに状態の行を消さなかった")

        release.clear()                 # 記録先がまた応答しなくなる
        self._feed(w, fed, 12)
        self.assertTrue(self._wait_until(lambda: self._lag_line(w), 2.0),
                        "追いついたあとにまた上限を超えたのに、知らせなかった")
        _pump(300)
        self.assertTrue(self._lag_line(w), "詰まったままなのに状態の行を消した")
        self.assertEqual(self.warning.call_count, 0, "遅れを警告の窓で出した")

    def test_a_link_with_a_gate_is_still_held_back_without_the_serial_warning(self):
        """関所のある接続は、これまでどおり描画と受信を止め、シリアル向けの遅れの知らせ（メモリに溜めて…）は出さないこと。

        止めたことの知らせ（状態の行）は、test_log_recording_io_stall_hold_notice.py が確かめる。
        """
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
                         "受信を止められる接続にも、シリアル向けの遅れの警告を出した")
        self.assertIsNone(self._lag_line(w),
                          "受信を止められる接続にも、シリアル向けの遅れの行を出した")
        release.set()
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and not w._pending_output.get("ser"), 10.0),
            "詰まりが解けても受信・描画を再開しなかった")
        self.assertEqual(self.warning.call_count, 0)

    def test_a_link_with_a_gate_over_the_limit_but_not_held_gets_no_serial_line(self):
        """関所のある接続で、書き込み待ちが上限を超えたまま受信を止めていない間も、シリアル向けの遅れ（メモリに溜めて…）として数えず、行も出さないこと。

        受信を止めている間は止めている行が優先されるので、上のテストでは遅れの
        行を出す作りかどうかを見分けられない（7 周目の検査役の変異 lag_on_gate:
        見回りの「関所の無い接続だけ」の条件を外しても通った）。止めるのは描き
        待ちがあるとき（_flush_pending_output）なので、1 回で描き切れる量ずつ
        渡して描き切るのを待ち、描き待ちが空のまま（受信が途切れたまま）
        書き込み待ちを上限より上にしてから見回りを回す。
        """
        from core import log_recording
        w = self._widget()
        release = threading.Event()
        self.addCleanup(release.set)
        path = self._start(w, release, limit=30.0)
        gate = w.output_gate("ser")     # SSH / Telnet と同じく関所を渡す
        lines = w.OUTPUT_SLICE // len(_chunk(0, 1))   # 1 回で描き切れる行数

        fed = 0
        end = time.perf_counter() + 8.0
        while w._log_backlog("ser") < self.HIGH and time.perf_counter() < end:
            w.queue_output("ser", _chunk(fed, lines))
            fed += lines
            self._wait_until(lambda: not w._pending_output.get("ser"), 2.0)
        self.assertGreaterEqual(w._log_backlog("ser"), self.HIGH,
                                "前提: 書き込み待ちが上限を超えた")
        self.assertFalse(w._pending_output.get("ser"), "前提: 描き待ちが無い")
        self.assertNotIn("ser", w._log_throttled, "前提: 受信を止めていない")
        self.assertTrue(gate.is_set(), "前提: 関所は開いている")

        w._check_log_writers()
        _pump(300)                      # 見回りの刻み（100ms）も何回か回す
        self.assertGreaterEqual(w._log_backlog("ser"), self.HIGH,
                                "前提: 書き込み待ちは上限を超えたまま")
        self.assertNotIn("ser", w._log_lagging,
                         "受信を止められる接続を、シリアルの遅れとして数えた")
        self.assertIsNone(self._lag_line(w),
                          "受信を止められる接続にも、シリアル向けの遅れの行を出した")
        self.assertEqual(self._line(w), "", "止めていないのに状態の行を出した")

        release.set()
        self.assertTrue(self._wait_until(
            lambda: w._log_backlog("ser") == 0, 10.0),
            "詰まりが解けても記録待ちが書き終わらない")
        w.stop_log_recording("ser")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), _chunk(0, fed).replace("\r\n", "\n"),
                             "詰まっている間の記録が欠けた・崩れた")
        self.assertIn("L%06d" % (fed - 1), w._terminals["ser"].toPlainText())
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
