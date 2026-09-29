"""記録先の詰まりで関所のある接続（SSH / Telnet）の受信を止めたら、一度だけ知らせることを検証する（4 周目 term の (1)）。

何が起きていたか（b2858c4 で実測。scratchpad\\cx132e-term\\probe_hold.py。
上限 256 KiB / 64 KiB）: 記録待ちが上限を超えると、関所のある接続はその機器の
描画を止めて受信の関所を閉じる（termui-03 Q1 (b)）。このとき利用者への知らせが
何も出なかった。止めて 3 秒たっても警告は []、画面の案内も []、記録中ダイアログは
「記録したバイト数: 0 バイト」のまま。打った 'show clock\\r' は機器へ送られたが、
エコーは描かれない。知らせが出るのは関所の無いシリアルだけだった
（_check_log_writers の `name not in self._output_gates`）。利用者は止まった理由が
分からず、エコーの見えないまま打ち直すと機器へ二重に送る。

どう直したか（作り手の親の決定）: 関所のある接続でも、止めたときに一度だけ
『記録先への書き込みが遅れているため、この機器の受信を止めています』の趣旨を
その機器の名前つきで知らせる。仕組みはシリアルの知らせに合わせた（見回りが
出す。知らせた機器は _log_lag_noticed に入れ、書き込み待ちが PENDING_LOW_WATER
まで減ったら、次に止めたときにまた一度知らせる）。止めている間も機器側が
待つので記録にも画面にも欠けは無く、メモリも増え続けないので、シリアルの
警告（QMessageBox.warning）ではなく案内（QMessageBox.information）で出す。
シリアル向けの『画面は進めたまま…』の警告は、関所のある接続には出さない
（tests/test_log_recording_io_stall_no_gate.py）。

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


class LogRecordingIoStallHoldNoticeTest(unittest.TestCase):
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
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-notice-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.information = mock.patch(
            "PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
        w.create_terminal_tab("dev")
        w.tab_widget.setCurrentWidget(w._terminals["dev"])
        return w

    def _start(self, w):
        path = os.path.join(self.dir, "dev.log")
        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if isinstance(file, str) and file == path and "w" in mode:
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

    def _feed_until_held(self, w, gate, first):
        """受信スレッドの代わり: 関所が開いている間だけ受信を渡し、閉じたら止める。"""
        fed = first
        end = time.perf_counter() + 4.0
        while gate.is_set() and time.perf_counter() < end:
            w.queue_output("dev", _chunk(fed, self.LINES))
            fed += self.LINES
            _pump(20)
        self.assertFalse(gate.is_set(), "前提: 記録待ちが上限を超えて受信を止めた")
        self.assertIn("dev", w._log_throttled, "前提: この機器の描画を止めている")
        return fed

    def _hold_notices(self):
        return [c for c in self.information.call_args_list
                if len(c.args) > 2 and "受信を止めて" in str(c.args[2])]

    def _caught_up(self, w, gate):
        return (gate.is_set() and not w._pending_output.get("dev")
                and w._log_backlog("dev") == 0)

    def test_holding_a_device_with_a_gate_notifies_once(self):
        """関所のある接続の受信を止めたら、その機器の名前つきで一度だけ知らせること。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w)
        gate = w.output_gate("dev")
        fed = self._feed_until_held(w, gate, 0)

        self.assertTrue(self._wait_until(lambda: self._hold_notices(), 2.0),
                        "記録先の詰まりで受信を止めたのに、何も知らせなかった")
        notice = self._hold_notices()[0]
        self.assertEqual(notice.args[1], "ログ記録")
        self.assertIn("dev", notice.args[2])
        self.assertIn("記録先への書き込みが遅れている", notice.args[2])
        # 解く手立てと、打った文字の行き先を案内する
        self.assertIn("記録を停止", notice.args[2])
        self.assertIn("打った文字", notice.args[2])

        # 止めたままでも、知らせは一度だけ
        _pump(500)
        self.assertTrue(gate.is_set() is False and "dev" in w._log_throttled,
                        "前提: まだ止めている")
        self.assertEqual(len(self._hold_notices()), 1, "知らせを繰り返した")
        self.assertEqual(self.warning.call_count, 0,
                         "関所のある接続に、シリアル向けの警告などを出した")

        self.release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate), 10.0),
                        "詰まりが解けても受信・描画を再開しなかった")
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), _chunk(0, fed).replace("\r\n", "\n"),
                             "止めている間の記録が欠けた・崩れた")
        self.assertIn("L%06d" % (fed - 1), w._terminals["dev"].toPlainText())
        self.assertEqual(len(self._hold_notices()), 1)
        self.assertEqual(self.information.call_count, 1, "ほかの案内が出た")

    def test_a_new_hold_after_catching_up_notifies_once_more(self):
        """書き込み待ちが追いついたあと、もう一度止めたら、また一度だけ知らせること。"""
        w = self._widget()
        self._start(w)
        gate = w.output_gate("dev")
        fed = self._feed_until_held(w, gate, 0)
        self.assertTrue(self._wait_until(lambda: self._hold_notices(), 2.0),
                        "記録先の詰まりで受信を止めたのに、何も知らせなかった")
        self.release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate), 10.0),
                        "前提: 追いついた")
        _pump(300)
        self.assertEqual(len(self._hold_notices()), 1, "追いつくまでに知らせを繰り返した")

        self.release.clear()            # 記録先がまた応答しなくなる
        self._feed_until_held(w, gate, fed)
        self.assertTrue(self._wait_until(
            lambda: len(self._hold_notices()) == 2, 2.0),
            "追いついたあとにまた受信を止めたのに、知らせなかった")
        _pump(300)
        self.assertEqual(len(self._hold_notices()), 2, "知らせを繰り返した")
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
