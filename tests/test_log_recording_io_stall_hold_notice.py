"""記録先の詰まりで関所のある接続（SSH / Telnet）の受信を止めている間、記録中ダイアログの状態の行でそれを知らせることを検証する（4 周目 term の (1)）。

何が起きていたか（b2858c4 で実測。scratchpad\\cx132e-term\\probe_hold.py。
上限 256 KiB / 64 KiB）: 記録待ちが上限を超えると、関所のある接続はその機器の
描画を止めて受信の関所を閉じる（termui-03 Q1 (b)）。このとき利用者への知らせが
何も出なかった。止めて 3 秒たっても警告は []、画面の案内も []、記録中ダイアログは
「記録したバイト数: 0 バイト」のまま。打った 'show clock\\r' は機器へ送られたが、
エコーは描かれない。知らせが出るのは関所の無いシリアルだけだった
（_check_log_writers の `name not in self._output_gates`）。利用者は止まった理由が
分からず、エコーの見えないまま打ち直すと機器へ二重に送る。

どう直したか（作り手の親の決定）: 関所のある接続でも、止めている間は
『記録先への書き込みが遅れているため、受信を止めています』の趣旨を知らせる
（見回りが出す）。止めている間も機器側が待つので記録にも画面にも欠けは無い。
シリアル向けの『メモリに溜めています』の知らせは、関所のある接続には出さない
（tests/test_log_recording_io_stall_no_gate.py）。
初めは QMessageBox.information（モーダル）で出し、次にモーダルでない QMessageBox を
1 回の記録で一度だけ show() で出していた（5 周目。
tests/test_log_recording_io_stall_hold_notice_modeless.py）。どちらの窓も出た瞬間に
キーの行き先を奪ったので（6 周目の検査役の指摘。
tests/test_log_recording_io_stall_status_line.py）、いまは窓を出さず、記録中
ダイアログ（LogRecordingDialog）の状態の行に、止めている間だけ出す。再開したら
消し、同じ記録でまた止めたら、また出す。記録を停止したら、ダイアログごと消える。

このテストでは上限をインスタンスで小さくして（256 KiB / 64 KiB）確かめる。
information・warning・QMessageBox.exec は開かずにすぐ戻る偽物に替え、
QMessageBox.show は数えてから本物を呼ぶ。窓を出したら、どれかの数で落ちる。
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
        # 出た窓を、出た順に（題, 本文, モーダルか）で覚える
        self.shown = []
        self.boxes = []
        # モーダルで出した窓（information、または exec）も覚える。どちらも
        # 開かずにすぐ戻す（本物は利用者が閉じるまで戻らないので、モーダルへ
        # 戻る退行があるとテストが止まったままになる）
        self.execs = []

        def recording_exec(box, *args):
            self.execs.append(box.text())
            self.shown.append((box.windowTitle(), box.text(), True))
            return QMessageBox.StandardButton.Ok.value
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.information = mock.patch(
            "PyQt6.QtWidgets.QMessageBox.information",
            side_effect=lambda parent, title, text, *a, **k:
            self.shown.append((title, text, True))).start()
        mock.patch.object(QMessageBox, "exec", recording_exec).start()
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
        """開いたままの窓を、後のテストへ残さない。"""
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

    @staticmethod
    def _line(w):
        """dev の記録中ダイアログの状態の行（表に出ていなければ ""。ダイアログか行が無ければ None）"""
        dialog = w._log_dialogs.get("dev")
        label = getattr(dialog, "status_label", None)
        if label is None:
            return None
        return label.text() if label.isVisibleTo(dialog) else ""

    def _held_line(self, w):
        """いま出ている『受信を止めて』の行（出ていなければ None）"""
        line = self._line(w)
        return line if line and "受信を止めて" in line else None

    def _assert_no_window(self):
        self.assertEqual(self.shown, [], "記録先の詰まりを知らせるのに窓を出した")
        self.assertEqual(self.information.call_count, 0, "モーダルの窓を出した")
        self.assertEqual(self.execs, [], "窓を exec のモーダルで出した")
        self.assertEqual(self.warning.call_count, 0,
                         "関所のある接続に、シリアル向けの警告などを出した")

    def _caught_up(self, w, gate):
        return (gate.is_set() and not w._pending_output.get("dev")
                and w._log_backlog("dev") == 0)

    def test_holding_a_device_with_a_gate_shows_the_status_line(self):
        """関所のある接続の受信を止めている間、その機器の記録中ダイアログに状態の行を出し、再開したら消すこと。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w)
        self.assertEqual(self._line(w), "", "前提: 記録を始めたときは状態の行が無い")
        gate = w.output_gate("dev")
        fed = self._feed_until_held(w, gate, 0)

        self.assertTrue(self._wait_until(lambda: self._held_line(w), 2.0),
                        "記録先の詰まりで受信を止めたのに、何も知らせなかった: %r"
                        % self._line(w))
        self.assertEqual(w._log_dialogs["dev"].device_name, "dev")
        line = self._held_line(w)
        self.assertIn("記録先への書き込みが遅れている", line)
        # 欠けないことと、再開すること、打った文字の行き先を案内する
        self.assertIn("欠けは出ません", line)
        self.assertIn("再開します", line)
        self.assertIn("打った文字", line)
        self.assertNotIn("メモリに溜めて", line, "シリアル向けの知らせを出した")

        # 止めている間は出たまま
        _pump(500)
        self.assertTrue(gate.is_set() is False and "dev" in w._log_throttled,
                        "前提: まだ止めている")
        self.assertTrue(self._held_line(w), "止めている間に状態の行を消した")
        self._assert_no_window()

        self.release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate), 10.0),
                        "詰まりが解けても受信・描画を再開しなかった")
        self.assertTrue(self._wait_until(lambda: self._line(w) == "", 2.0),
                        "再開したのに状態の行を消さなかった: %r" % self._line(w))
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), _chunk(0, fed).replace("\r\n", "\n"),
                             "止めている間の記録が欠けた・崩れた")
        self.assertIn("L%06d" % (fed - 1), w._terminals["dev"].toPlainText())
        self._assert_no_window()

    def test_every_hold_shows_the_line_until_it_resumes(self):
        """追いついてからまた止めたら同じ記録でもまた出し、記録を始め直して止めたら新しいダイアログに出すこと。"""
        w = self._widget()
        self._start(w)
        gate = w.output_gate("dev")
        fed = self._feed_until_held(w, gate, 0)
        self.assertTrue(self._wait_until(lambda: self._held_line(w), 2.0),
                        "記録先の詰まりで受信を止めたのに、何も知らせなかった")
        self.release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate), 10.0),
                        "前提: 追いついた")
        self.assertTrue(self._wait_until(lambda: self._line(w) == "", 2.0),
                        "追いついたのに状態の行を消さなかった")

        self.release.clear()            # 記録先がまた応答しなくなる
        fed = self._feed_until_held(w, gate, fed)
        self.assertTrue(self._wait_until(lambda: self._held_line(w), 2.0),
                        "同じ記録のうちにまた止めたのに、状態の行を出さなかった")

        # 記録を停止すると止めが解け（記録先は詰まったまま）、記録中ダイアログも
        # 消える。別のファイルへ始め直して、また止まったら、新しいダイアログに出す
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and "dev" not in w._log_throttled, 3.0),
            "前提: 記録を停止したら止めが解けた")
        self.assertIsNone(self._line(w), "前提: 記録中ダイアログが消えた")
        self._start(w, "dev2.log")
        self.assertEqual(self._line(w), "", "始め直した記録に、前の記録の行を出した")
        self._feed_until_held(w, gate, fed)
        self.assertTrue(self._wait_until(lambda: self._held_line(w), 2.0),
                        "記録を始め直してまた受信を止めたのに、知らせなかった")
        _pump(300)
        self.assertTrue(self._held_line(w))
        self._assert_no_window()


if __name__ == "__main__":
    unittest.main()
