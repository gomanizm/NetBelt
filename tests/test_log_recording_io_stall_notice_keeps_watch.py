"""見回りの知らせ（遅れの案内・警告、書き込みの失敗の警告）が開いている間も、止めた機器を再開させることを検証する（4 周目 term の (1) の手直し）。

何が起きていたか（123bde6 で実測。scratchpad\\cx132e-term-fix2\\probe_modal.py と
probe_serial_modal.py。上限 256 KiB / 64 KiB）: 見回り（_check_log_writers）は、
100ms ごとの QTimer（_log_watch）のスロットの中で、知らせをモーダルで出していた。
Qt は、スロットが入れ子のイベントループにいる間、同じタイマーを配り直さない。
そのため知らせが開いている間は、見回りが一度も動かなかった（案内が開いていた
0.44〜5.99 秒の呼び出しは 0 回）。記録先は 1.5 秒に戻り、書き込み待ちは 0 まで
減った。それでも止めた機器は、利用者が OK を押すまで再開しなかった（関所は閉じた
まま）。席を外した長い取得が進まず、案内の『記録先が応答すると再開します』も
成り立たない。シリアルの遅れの警告と、書き込みの失敗の警告でも、別のタブの止めた
機器が同じように再開しなかった。

どう直したか: 見回りの本体は、刻みのタイマーから単発のタイマー（_log_check）へ
渡して動かす。単発のタイマーは刻みのたびに掛け直すので、知らせが開いている間も、
次の刻みで本体が動く。同じ機器の遅れの知らせが開いている間は、その機器へは
出し直さない（止めては再開する遅い記録先で、モーダルが入れ子に積み上がらない
ように）。止めたことの案内を出すかは、機器ごとに止めているかを読み直して決める
（ほかの機器の知らせが開いている間に再開した機器へ、閉じたあとで『受信を止めて
います』と知らせない）。

その後（5 周目の検査役の指摘。tests/test_log_recording_io_stall_hold_notice_modeless.py）、
止めたことの案内はモーダルでない QMessageBox を show() で出すようにし、1 回の
記録で一度だけにした。モーダルのまま残るのは警告（シリアルの遅れ、書き込みの
失敗）。止めたことの案内のテストは、案内を開いたままにして同じ性質（開いている
間も再開する、開いている間も閉じたあとも出し直さない）を確かめる。ほかの機器の
モーダルが開いている間に再開した機器へ知らせないことは、シリアルの遅れの警告を
開いた形で確かめる。

警告は、入れ子のイベントループを回してから戻る偽物に替える（本物のモーダルと
同じく、開いている間は呼んだスロットへ戻らない）。止めたことの案内は
QMessageBox.show を見張って数える。information と QMessageBox.exec は、開かずに
すぐ戻る偽物に替え、モーダルで出た案内として数える（モーダルへ戻る退行が
あっても、止まったままにならずに落ちる）。上限はインスタンスで小さくする。
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
    ならないように）。fail を渡すと、放されたあとの write はその例外で失敗する。
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


def _chunk(name, first, count):
    return "".join("%s%06d %s\r\n" % (name, i, "y" * 50)
                   for i in range(first, first + count))


class LogRecordingNoticeKeepsWatchTest(unittest.TestCase):
    HIGH = 256 * 1024
    LOW = 64 * 1024
    LINES = 512            # 1 回に受信させる行数（約 30 KB）
    OPEN_FOR = 5.0         # 知らせを開いたままにする上限（秒）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtWidgets import QMessageBox
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-keepwatch-")
        # 出た案内を、出た順に覚える（題, 本文, モーダルか）
        self.shown = []
        self.boxes = []
        # モーダルで出した案内（information、または exec）も覚える。どちらも
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
        self.fed = {}

    def _close_boxes(self):
        """開いたままの案内を閉じる（利用者が閉じる。後のテストへ残さない）。"""
        for box in self.boxes:
            try:
                if box.isVisible():
                    box.accept()
            except RuntimeError:
                pass            # 閉じて消えた

    def _hold_notices(self):
        """出た『受信を止めて』の案内（題, 本文, モーダルか）"""
        return [shown for shown in self.shown if "受信を止めて" in shown[1]]

    def _open_hold_boxes(self):
        """いま開いている『受信を止めて』の案内"""
        opened = []
        for box in self.boxes:
            try:
                if box.isVisible() and "受信を止めて" in box.text():
                    opened.append(box)
            except RuntimeError:
                pass
        return opened

    def _wait_for_open_hold_box(self):
        """止めたことの案内が開くのを待って返す（モーダルで出たら落とす）。"""
        def modal_notices():
            return [text for _, text, modal in self._hold_notices() if modal]
        self._wait_until(lambda: self._open_hold_boxes() or modal_notices(), 2.0)
        self.assertEqual(modal_notices(), [], "案内をモーダルで出した")
        self.assertTrue(self._open_hold_boxes(), "前提: 止めたことの案内が開いた")
        return self._open_hold_boxes()[0]

    def _widget(self, names):
        from core import log_recording
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
        for name in names:
            self.addCleanup(log_recording.stop, name)
            w.create_terminal_tab(name)
        return w

    def _start(self, w, name, release, fail=None):
        path = os.path.join(self.dir, name + ".log")
        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if isinstance(file, str) and file == path and "w" in mode:
                return _StallingFile(f, release, 30.0, fail)
            return f

        w.tab_widget.setCurrentWidget(w._terminals[name])
        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn(name, w._log_files, "前提: 記録が始まっている")
        return path

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def _feed_until_held(self, w, gate, name, stop=lambda: False):
        """受信スレッドの代わり: 関所が開いている間だけ受信を渡し、閉じたら止める。

        渡した行数は self.fed に数える。stop() が真になっても止める（知らせの
        偽物が、開いている間に再開させたあと、ここへ戻ってきたとき）。
        """
        end = time.perf_counter() + 4.0
        while gate.is_set() and not stop() and time.perf_counter() < end:
            first = self.fed.get(name, 0)
            w.queue_output(name, _chunk(name, first, self.LINES))
            self.fed[name] = first + self.LINES
            _pump(20)

    def _caught_up(self, w, gate, name):
        return (gate.is_set() and name not in w._log_throttled
                and not w._pending_output.get(name)
                and w._log_backlog(name) == 0)

    def _open_until_resumed(self, w, gate, seen, text):
        """知らせの偽物の中身: 開いたまま記録先を戻し、閉じる前に再開するかを見る。"""
        seen["text"] = text
        seen["held"] = "dev" in w._log_throttled and not gate.is_set()
        _pump(200)
        self.release.set()
        seen["resumed"] = self._wait_until(
            lambda: self._caught_up(w, gate, "dev"), self.OPEN_FOR)

    def _assert_recorded_in_full(self, w, path, name):
        from core import log_recording
        w.stop_log_recording(name)
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(
                f.read(), _chunk(name, 0, self.fed[name]).replace("\r\n", "\n"),
                "記録が欠けた・崩れた")
        self.assertIn("%s%06d" % (name, self.fed[name] - 1),
                      w._terminals[name].toPlainText())

    def test_the_device_resumes_while_its_hold_notice_is_open(self):
        """止めたことの案内が開いている間に記録先が戻ったら、閉じるのを待たずに再開すること。"""
        w = self._widget(["dev"])
        path = self._start(w, "dev", self.release)
        gate = w.output_gate("dev")
        self._feed_until_held(w, gate, "dev")
        box = self._wait_for_open_hold_box()
        seen = {}
        # 案内は開いたまま（利用者は閉じていない）、記録先が戻る
        self._open_until_resumed(w, gate, seen, box.text())
        self.assertTrue(seen["held"], "前提: 案内が開いたときは止めていた")
        self.assertTrue(box.isVisible(), "前提: 再開を見届けるまで案内は開いたまま")
        self.assertTrue(seen["resumed"],
                        "案内が開いている間は見回りが動かず、記録先が戻っても、"
                        "閉じるまで受信・描画を再開しなかった")
        self.assertEqual(len(self._hold_notices()), 1)
        self.assertEqual(self.information.call_count, 0, "案内をモーダルで出した")
        self.assertEqual(self.execs, [], "案内を exec のモーダルで出した")
        self.assertEqual(self.warning.call_count, 0)
        self._assert_recorded_in_full(w, path, "dev")

    def test_a_held_device_resumes_while_the_serial_lag_warning_is_open(self):
        """シリアルの遅れの警告が開いている間に、別のタブの止めた機器の記録先が戻ったら、再開すること。"""
        w = self._widget(["ser", "dev"])
        ser_release = threading.Event()
        self.addCleanup(ser_release.set)
        self._start(w, "ser", ser_release)      # 関所の無い接続（シリアルの形）
        path = self._start(w, "dev", self.release)
        gate = w.output_gate("dev")             # dev だけ関所あり
        self._feed_until_held(w, gate, "dev")
        self.assertFalse(gate.is_set(), "前提: dev の受信を止めた")
        self.assertNotIn("ser", w._output_gates, "前提: ser には関所が無い")
        seen = {}
        self.warning.side_effect = (
            lambda parent, title, text, *a, **k:
            self._open_until_resumed(w, gate, seen, text))

        # シリアルの受信は止まらない。記録待ちが上限を超えて警告が出るまで渡す
        end = time.perf_counter() + 4.0
        while not seen and time.perf_counter() < end:
            first = self.fed.get("ser", 0)
            w.queue_output("ser", _chunk("ser", first, self.LINES))
            self.fed["ser"] = first + self.LINES
            _pump(20)
        self.assertTrue(self._wait_until(lambda: "resumed" in seen,
                                         self.OPEN_FOR + 3.0),
                        "前提: シリアルの遅れの警告が開いて閉じた")
        self.assertIn("ser", seen["text"])
        self.assertIn("画面は進めたまま", seen["text"])
        self.assertTrue(seen["held"], "前提: 警告が開いたときは dev を止めていた")
        self.assertTrue(seen["resumed"],
                        "シリアルの遅れの警告が開いている間は見回りが動かず、"
                        "別のタブの止めた機器が、記録先が戻っても閉じるまで"
                        "再開しなかった")
        self.assertEqual(self.warning.call_count, 1)
        self.assertEqual(self.execs, [], "案内を exec のモーダルで出した")
        self._assert_recorded_in_full(w, path, "dev")

    def test_a_held_device_resumes_while_a_write_failure_warning_is_open(self):
        """見回りが出す書き込みの失敗の警告が開いている間に、別のタブの止めた機器の記録先が戻ったら、再開すること。"""
        w = self._widget(["bad", "dev"])
        bad_release = threading.Event()
        self.addCleanup(bad_release.set)
        error = OSError(64, "The specified network name is no longer available")
        self._start(w, "bad", bad_release, fail=error)
        path = self._start(w, "dev", self.release)
        gate = w.output_gate("dev")
        w.queue_output("bad", "line\r\n")
        self.assertTrue(self._wait_until(lambda: w._log_files["bad"].busy, 2.0),
                        "前提: bad の記録先が詰まった")
        self._feed_until_held(w, gate, "dev")
        self.assertFalse(gate.is_set(), "前提: dev の受信を止めた")
        seen = {}
        self.warning.side_effect = (
            lambda parent, title, text, *a, **k:
            self._open_until_resumed(w, gate, seen, text))

        # bad の記録先が戻って書き込みが失敗する。次の受信は来ないので、
        # 見回りが見つけて警告する
        bad_release.set()
        self.assertTrue(self._wait_until(lambda: "resumed" in seen,
                                         self.OPEN_FOR + 3.0),
                        "前提: 書き込みの失敗の警告が開いて閉じた")
        self.assertIn("bad", seen["text"])
        self.assertIn("書き込みに失敗しました", seen["text"])
        self.assertTrue(seen["held"], "前提: 警告が開いたときは dev を止めていた")
        self.assertTrue(seen["resumed"],
                        "書き込みの失敗の警告が開いている間は見回りが動かず、"
                        "別のタブの止めた機器が、記録先が戻っても閉じるまで"
                        "再開しなかった")
        self.assertNotIn("bad", w._log_files, "失敗した記録が記録中のまま残った")
        self.assertEqual(self.warning.call_count, 1)
        self.assertEqual(self.execs, [], "案内を exec のモーダルで出した")
        self._assert_recorded_in_full(w, path, "dev")

    def test_a_device_held_again_while_its_notice_is_open_is_not_noticed_again(self):
        """案内が開いたまま、再開した機器がまた止まっても、案内を出し直さないこと（閉じたあとも、同じ記録のうちは出し直さない）。"""
        w = self._widget(["dev"])
        path = self._start(w, "dev", self.release)
        gate = w.output_gate("dev")
        self._feed_until_held(w, gate, "dev")
        box = self._wait_for_open_hold_box()
        seen = {}
        self._open_until_resumed(w, gate, seen, box.text())
        self.assertTrue(seen["resumed"],
                        "案内が開いている間は見回りが動かず、記録先が戻っても、"
                        "閉じるまで受信・描画を再開しなかった")
        # 開いたままの間に、記録先がまた応答しなくなって止まる
        # （遅い記録先では、止めては再開するのを繰り返す）
        self.release.clear()
        self._feed_until_held(w, gate, "dev")
        self.assertIn("dev", w._log_throttled, "前提: 案内が開いたまま、また止めた")
        _pump(500)          # 見回りが何回か動く
        self.assertTrue(box.isVisible(), "前提: 案内は開いたまま")
        self.assertEqual(len(self._hold_notices()), 1, "開いている間に案内を出し直した")
        self.assertEqual(self.information.call_count, 0, "案内をモーダルで出した")
        self.assertEqual(self.execs, [], "案内を exec のモーダルで出した")
        self._close_boxes()     # 利用者が閉じる
        _pump(300)
        self.assertEqual(len(self._hold_notices()), 1,
                         "閉じたあと、同じ記録のうちに案内を繰り返した")

        self.release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate, "dev"), 10.0),
                        "詰まりが解けても受信・描画を再開しなかった")
        self._assert_recorded_in_full(w, path, "dev")

    def test_a_device_resumed_while_another_notice_is_open_is_not_told_it_is_held(self):
        """ほかの機器の警告が開いている間に再開した機器へ、閉じたあとで『受信を止めています』と知らせないこと。"""
        w = self._widget(["ser", "dev"])
        ser_release = threading.Event()
        self.addCleanup(ser_release.set)
        self._start(w, "ser", ser_release)      # 関所の無い接続（シリアルの形）
        path = self._start(w, "dev", self.release)
        gate = w.output_gate("dev")             # dev だけ関所あり
        # dev を止め、ser の記録待ちが上限を超えるまで見回りの本体を動かさない
        # （同じ回の見回りで、ser の警告のあとに dev へ知らせる順番が来るようにする）
        w._log_check.blockSignals(True)
        self._feed_until_held(w, gate, "dev")
        end = time.perf_counter() + 4.0
        while (w._log_backlog("ser") < self.HIGH
               and time.perf_counter() < end):
            first = self.fed.get("ser", 0)
            w.queue_output("ser", _chunk("ser", first, self.LINES))
            self.fed["ser"] = first + self.LINES
            _pump(20)
        self.assertIn("dev", w._log_throttled, "前提: dev の受信を止めた")
        self.assertGreaterEqual(w._log_backlog("ser"), self.HIGH,
                                "前提: ser の記録待ちが上限を超えた")
        self.assertEqual(self._hold_notices(), [], "前提: まだ見回りは知らせていない")
        seen = {}

        def warn(parent, title, text, *args, **kwargs):
            seen["text"] = text
            seen["held"] = "dev" in w._log_throttled
            # 開いている間に dev の記録先が戻る。見回りより先に書き終える
            # （イベントループを回さずに待つ）ので、入り直した見回りは、
            # dev へ知らせる前に再開させる
            self.release.set()
            w._log_files["dev"].wait(3.0)
            seen["resumed"] = self._wait_until(
                lambda: self._caught_up(w, gate, "dev"), self.OPEN_FOR)
        self.warning.side_effect = warn
        w._log_check.blockSignals(False)

        self.assertTrue(self._wait_until(lambda: "resumed" in seen,
                                         self.OPEN_FOR + 3.0),
                        "前提: シリアルの遅れの警告が開いて閉じた")
        self.assertIn("ser", seen["text"])
        self.assertIn("画面は進めたまま", seen["text"])
        self.assertTrue(seen["held"], "前提: 警告が開いたときは dev を止めていた")
        self.assertTrue(seen["resumed"],
                        "警告が開いている間は見回りが動かず、dev が、"
                        "記録先が戻っても閉じるまで再開しなかった")
        _pump(300)
        self.assertEqual(self._hold_notices(), [],
                         "警告を閉じたあと、もう再開した機器へ『受信を止めて"
                         "います』と知らせた")
        self.assertEqual(self.warning.call_count, 1)
        self.assertEqual(self.information.call_count, 0)
        self.assertEqual(self.execs, [], "案内を exec のモーダルで出した")
        self._assert_recorded_in_full(w, path, "dev")


if __name__ == "__main__":
    unittest.main()
