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

知らせは、入れ子のイベントループを回してから戻る偽物に替える（本物のモーダルと
同じく、開いている間は呼んだスロットへ戻らない）。上限はインスタンスで小さくする。
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
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-keepwatch-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.information = mock.patch(
            "PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.fed = {}

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
        seen = {}
        self.information.side_effect = (
            lambda parent, title, text, *a, **k:
            self._open_until_resumed(w, gate, seen, text))

        self._feed_until_held(w, gate, "dev", lambda: bool(seen))
        self.assertTrue(self._wait_until(lambda: "resumed" in seen,
                                         self.OPEN_FOR + 3.0),
                        "前提: 止めたことの案内が開いて閉じた")
        self.assertIn("受信を止めて", seen["text"])
        self.assertTrue(seen["held"], "前提: 案内が開いたときは止めていた")
        self.assertTrue(seen["resumed"],
                        "案内が開いている間は見回りが動かず、記録先が戻っても、"
                        "閉じるまで受信・描画を再開しなかった")
        self.assertEqual(self.information.call_count, 1)
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
        self._assert_recorded_in_full(w, path, "dev")

    def test_a_device_held_again_while_its_notice_is_open_is_not_noticed_again(self):
        """案内が開いたまま、再開した機器がまた止まっても、案内を入れ子に出し直さないこと。"""
        w = self._widget(["dev"])
        path = self._start(w, "dev", self.release)
        gate = w.output_gate("dev")
        seen = {"depth": 0, "max_depth": 0}

        def notice(parent, title, text, *args, **kwargs):
            seen["depth"] += 1
            seen["max_depth"] = max(seen["max_depth"], seen["depth"])
            try:
                if seen["depth"] > 1:
                    return
                self._open_until_resumed(w, gate, seen, text)
                if not seen["resumed"]:
                    return
                # 開いたままの間に、記録先がまた応答しなくなって止まる
                # （遅い記録先では、止めては再開するのを繰り返す）
                self.release.clear()
                self._feed_until_held(w, gate, "dev")
                seen["held_again"] = "dev" in w._log_throttled
                _pump(500)          # 見回りが何回か動く
                seen["notices_while_open"] = self.information.call_count
            finally:
                seen["depth"] -= 1
        self.information.side_effect = notice

        self._feed_until_held(w, gate, "dev", lambda: "resumed" in seen)
        self.assertTrue(self._wait_until(lambda: seen["depth"] == 0
                                         and "resumed" in seen,
                                         self.OPEN_FOR + 8.0),
                        "前提: 止めたことの案内が開いて閉じた")
        self.assertTrue(seen["resumed"],
                        "案内が開いている間は見回りが動かず、記録先が戻っても、"
                        "閉じるまで受信・描画を再開しなかった")
        self.assertTrue(seen["held_again"], "前提: 案内が開いたまま、また止めた")
        self.assertEqual(seen["max_depth"], 1, "開いている案内の上に、案内を入れ子に出した")
        self.assertEqual(seen["notices_while_open"], 1, "開いている間に案内を出し直した")
        _pump(300)
        self.assertEqual(self.information.call_count, 1,
                         "閉じたあと、同じ止めについて案内を繰り返した")

        self.release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate, "dev"), 10.0),
                        "詰まりが解けても受信・描画を再開しなかった")
        self._assert_recorded_in_full(w, path, "dev")

    def test_a_device_resumed_while_another_notice_is_open_is_not_told_it_is_held(self):
        """ほかの機器の案内が開いている間に再開した機器へ、閉じたあとで『受信を止めています』と知らせないこと。"""
        names = ("dev1", "dev2")
        w = self._widget(names)
        releases = {"dev1": self.release, "dev2": threading.Event()}
        self.addCleanup(releases["dev2"].set)
        paths = {name: self._start(w, name, releases[name]) for name in names}
        gates = {name: w.output_gate(name) for name in names}
        # 両方を止めるまで見回りの本体を動かさない（同じ回の見回りで、両方へ
        # 知らせる順番が来るようにする）
        w._log_check.blockSignals(True)
        for name in names:
            self._feed_until_held(w, gates[name], name)
        self.assertTrue(all(name in w._log_throttled for name in names),
                        "前提: 両方の受信を止めた")
        noticed = []
        seen = {}

        def notice(parent, title, text, *args, **kwargs):
            name = text.split(" ", 1)[0]
            noticed.append((name, name in w._log_throttled))
            if len(noticed) > 1:
                return
            other = "dev2" if name == "dev1" else "dev1"
            # 開いている間に、もう一方の記録先が戻る。見回りより先に書き終える
            # （イベントループを回さずに待つ）ので、入り直した見回りは、
            # もう一方へ知らせる前に再開させる
            releases[other].set()
            w._log_files[other].wait(3.0)
            seen["resumed"] = self._wait_until(
                lambda: self._caught_up(w, gates[other], other), self.OPEN_FOR)
        self.information.side_effect = notice
        w._log_check.blockSignals(False)

        self.assertTrue(self._wait_until(lambda: "resumed" in seen,
                                         self.OPEN_FOR + 3.0),
                        "前提: 止めたことの案内が開いて閉じた")
        self.assertTrue(seen["resumed"],
                        "案内が開いている間は見回りが動かず、もう一方の機器が、"
                        "記録先が戻っても閉じるまで再開しなかった")
        _pump(300)
        self.assertEqual([name for name, held in noticed if not held], [],
                         "案内を閉じたあと、もう再開した機器へ『受信を止めて"
                         "います』と知らせた")
        self.assertEqual(len(noticed), 1)

        for name in names:
            releases[name].set()
        for name in names:
            self.assertTrue(self._wait_until(
                lambda: self._caught_up(w, gates[name], name), 10.0),
                "詰まりが解けても受信・描画を再開しなかった")
            self._assert_recorded_in_full(w, paths[name], name)


if __name__ == "__main__":
    unittest.main()
