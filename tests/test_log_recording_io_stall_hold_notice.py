"""記録先の詰まりで関所のある接続（SSH / Telnet）の受信を止めたら、一度だけ知らせることを検証する（4 周目 term の (1)）。

何が起きていたか（b2858c4 で実測。scratchpad\\cx132e-term\\probe_hold.py。
上限 256 KiB / 64 KiB）: 記録待ちが上限を超えると、関所のある接続はその機器の
描画を止めて受信の関所を閉じる（termui-03 Q1 (b)）。このとき利用者への知らせが
何も出なかった。止めて 3 秒たっても警告は []、画面の案内も []、記録中ダイアログは
「記録したバイト数: 0 バイト」のまま。打った 'show clock\\r' は機器へ送られたが、
エコーは描かれない。知らせが出るのは関所の無いシリアルだけだった
（_check_log_writers の `name not in self._output_gates`）。利用者は止まった理由が
分からず、エコーの見えないまま打ち直すと機器へ二重に送る。

どう直したか（作り手の親の決定）: 関所のある接続でも、止めたときに
『記録先への書き込みが遅れているため、この機器の受信を止めています』の趣旨を
その機器の名前つきで知らせる（見回りが出す）。止めている間も機器側が
待つので記録にも画面にも欠けは無く、メモリも増え続けないので、シリアルの
警告（QMessageBox.warning）ではなく案内で出す。
シリアル向けの『画面は進めたまま…』の警告は、関所のある接続には出さない
（tests/test_log_recording_io_stall_no_gate.py）。
初めはシリアルと同じ数え方（書き込み待ちが PENDING_LOW_WATER まで減ったら、
次に止めたときにまた知らせる）で、QMessageBox.information（モーダル）で出して
いた。遅いが応答する記録先では止めるたびにモーダルが出直したので（5 周目の
検査役の指摘。tests/test_log_recording_io_stall_hold_notice_modeless.py）、
いまは 1 回の記録（start_log_recording から停止・中止まで）で一度だけ、
モーダルでない案内（QMessageBox を show()）で出す。追いついてからまた止めても
同じ記録のうちは出し直さず、記録を始め直したら、また一度出す。

このテストでは上限をインスタンスで小さくして（256 KiB / 64 KiB）確かめる。
案内は QMessageBox.show を見張って数える（モーダルの information は出ない）。
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
        from PyQt6.QtWidgets import QMessageBox
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.information = mock.patch(
            "PyQt6.QtWidgets.QMessageBox.information").start()
        # show() で出た案内を、出た順に（題, 本文, モーダルか）で覚える
        self.shown = []
        self.boxes = []
        real_show = QMessageBox.show

        def recording_show(box):
            self.shown.append((box.windowTitle(), box.text(), box.isModal()))
            self.boxes.append(box)
            real_show(box)
        mock.patch.object(QMessageBox, "show", recording_show).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self._close_boxes)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _close_boxes(self):
        """開いたままの案内を、後のテストへ残さない。"""
        for box in self.boxes:
            try:
                if box.isVisible():
                    box.accept()
            except RuntimeError:
                pass            # 閉じて消えた

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
        w.create_terminal_tab("dev")
        w.tab_widget.setCurrentWidget(w._terminals["dev"])
        return w

    def _start(self, w, filename="dev.log"):
        path = os.path.join(self.dir, filename)
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
        """出た『受信を止めて』の案内（題, 本文, モーダルか）"""
        return [shown for shown in self.shown if "受信を止めて" in shown[1]]

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
        title, text, modal = self._hold_notices()[0]
        self.assertEqual(title, "ログ記録")
        self.assertIn("dev", text)
        self.assertIn("記録先への書き込みが遅れている", text)
        # 解く手立てと、打った文字の行き先を案内する
        self.assertIn("記録を停止", text)
        self.assertIn("打った文字", text)
        self.assertFalse(modal, "案内がモーダルで出た")

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
        self.assertEqual(len(self.shown), 1, "ほかの案内が出た")
        self.assertEqual(self.information.call_count, 0, "モーダルの案内が出た")

    def test_a_new_hold_is_noticed_again_only_in_a_new_recording(self):
        """追いついてからまた止めても同じ記録のうちは知らせ直さず、記録を始め直して止めたら、また一度だけ知らせること。"""
        w = self._widget()
        self._start(w)
        gate = w.output_gate("dev")
        fed = self._feed_until_held(w, gate, 0)
        self.assertTrue(self._wait_until(lambda: self._hold_notices(), 2.0),
                        "記録先の詰まりで受信を止めたのに、何も知らせなかった")
        self._close_boxes()             # 利用者が閉じる
        self.release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate), 10.0),
                        "前提: 追いついた")
        _pump(300)
        self.assertEqual(len(self._hold_notices()), 1, "追いつくまでに知らせを繰り返した")

        self.release.clear()            # 記録先がまた応答しなくなる
        fed = self._feed_until_held(w, gate, fed)
        _pump(500)
        self.assertEqual(len(self._hold_notices()), 1,
                         "同じ記録のうちに、また止めたことを知らせ直した")

        # 記録を停止すると止めが解ける（記録先は詰まったまま）。別のファイルへ
        # 始め直して、また止まったら、その記録では一度だけ知らせる
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and "dev" not in w._log_throttled, 3.0),
            "前提: 記録を停止したら止めが解けた")
        self._start(w, "dev2.log")
        self._feed_until_held(w, gate, fed)
        self.assertTrue(self._wait_until(
            lambda: len(self._hold_notices()) == 2, 2.0),
            "記録を始め直してまた受信を止めたのに、知らせなかった")
        _pump(300)
        self.assertEqual(len(self._hold_notices()), 2, "知らせを繰り返した")
        self.assertEqual([modal for _, _, modal in self._hold_notices()],
                         [False, False], "案内がモーダルで出た")
        self.assertEqual(self.information.call_count, 0)
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
