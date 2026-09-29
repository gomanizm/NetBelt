"""関所のある接続（SSH / Telnet）で受信を止めたことの案内を、1 回の記録で一度だけ、モーダルでなく出すことを検証する（5 周目 term の検査役の指摘）。

何が起きていたか（2ebc5b6 で実測。scratchpad\\cx132f-term\\logs の base_mine_cycle.txt と
base_mw_slow.txt。上限 256 KiB / 64 KiB）: 受信を止めたことの案内は
QMessageBox.information（exec のモーダル）で出ていた。一度だけ出すかを戻す条件
（書き込み待ちが PENDING_LOW_WATER まで減ったら _log_lag_noticed から外す）は、
関所のある接続では、止めて書き込み待ちが減るたびに満たされる。遅いが応答する
記録先では止めては再開するのを繰り返すので、止めるたびに案内が出直した
（1 回の書き込みに 0.15 秒かかる記録先で、20 秒に 3 回止めて案内も 3 回。
MainWindow と 127.0.0.1 の Telnet では、12 秒に 6 回止めて本物のモーダルが 6 回）。
開いている間は、モーダルがほかの窓への入力を遮るので、どのタブにも打てない。

どう直したか（作り手の親の決定）: 案内は、モーダルでない QMessageBox を show() で
出す（exec しない）。同じ機器の案内が開いている間は出し直さない。出すのは 1 回の
記録（start_log_recording から停止・中止まで）で一度だけで、止めては再開するのを
繰り返しても出し直さない。記録を始め直したら、また一度出せる。端末の中に書く
案内（show_notice）にはしない（描き待ちを先に描き切るので、止めている意味が
無くなる）。シリアルの警告（QMessageBox.warning）は変えない。

案内は、表に出ている QMessageBox を見張って見つける（モーダルで出ても、モーダルで
なく出ても見つかる）。利用者の代わりに、見つけてから決めた時間がたったら閉じる。
上限はインスタンスで小さくする（128 KiB / 32 KiB）。
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

HOLD_TEXT = "受信を止めて"


class _SlowFile:
    """書くたびに delay 秒かかるが、必ず応答する記録ファイル（応答の遅い共有フォルダの代わり）。"""

    def __init__(self, f, delay):
        self._f = f
        self.name = f.name
        self._delay = delay

    def write(self, text):
        time.sleep(self._delay)
        return self._f.write(text)

    def flush(self):
        return self._f.flush()

    def close(self):
        return self._f.close()

    @property
    def closed(self):
        return self._f.closed


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


def _open_notices(owner):
    """owner の、いま表に出ている『受信を止めて』の案内"""
    from PyQt6.QtWidgets import QApplication, QMessageBox
    return [widget for widget in QApplication.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()
            and widget.parentWidget() is owner and HOLD_TEXT in widget.text()]


class _NoticeWatch:
    """owner の『受信を止めて』の案内を見張って記録し、利用者の代わりに閉じる。

    close_after 秒たったら閉じる（None なら閉じない）。モーダルで出た案内は、
    close_after が None でも modal_close_after 秒で閉じる（直す前の作りで
    テストが止まったままにならないように）。モーダルの入れ子のループの中でも
    タイマーは動くので見つかる。on_open(box) は見つけたときに一度だけ呼ぶ。
    見つけた案内には番号を付ける（閉じて消えた案内と取り違えない）。
    """

    def __init__(self, owner, close_after=0.2, modal_close_after=3.0, on_open=None):
        from PyQt6.QtCore import QTimer
        self.owner = owner
        self.close_after = close_after
        self.modal_close_after = modal_close_after
        self.on_open = on_open
        self.mark = "netbelt_test_notice_%d" % id(self)
        self.seen = []          # [{"text", "modal", "at", "closed"}]
        self._timer = QTimer()
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._scan)
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def open_boxes(self):
        """いま表に出ている案内"""
        return _open_notices(self.owner)

    def _scan(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication
        now = time.perf_counter()
        for box in self.open_boxes():
            index = box.property(self.mark)
            if index is None:
                entry = {
                    "text": box.text(), "at": now, "closed": False,
                    "modal": (box.windowModality() != Qt.WindowModality.NonModal
                              or QApplication.activeModalWidget() is not None)}
                self.seen.append(entry)
                box.setProperty(self.mark, len(self.seen) - 1)
                if self.on_open is not None:
                    self.on_open(box)
                continue
            entry = self.seen[index]
            limit = self.close_after
            if entry["modal"] and limit is None:
                limit = self.modal_close_after
            if not entry["closed"] and limit is not None and now - entry["at"] >= limit:
                entry["closed"] = True
                box.accept()


def _pump(ms):
    """ms ミリ秒だけイベントループを回す。"""
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _chunk(first, count):
    return "".join("L%06d %s\r\n" % (i, "y" * 50) for i in range(first, first + count))


class LogRecordingHoldNoticeModelessTest(unittest.TestCase):
    HIGH = 128 * 1024
    LOW = 32 * 1024
    LINES = 512            # 1 回に受信させる行数（約 30 KB）
    # 遅い記録先の 1 回の書き込みにかかる秒数。LogWriter.STALL_WAIT（0.1 秒）より
    # 長くする。短いと GUI が書き終わりを待つので、書き込み待ちが溜まらない
    SLOW = 0.15

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtWidgets import QDialog, QMessageBox
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-modeless-")
        # 関所のある接続に警告は出ない（出たら数えて落とす）
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.addCleanup(mock.patch.stopall)
        # 案内を exec で出したら覚える（exec はモーダル）
        self.execs = []
        real_exec = QDialog.exec

        def recording_exec(dialog, *args):
            self.execs.append(dialog.windowTitle())
            return real_exec(dialog, *args)
        mock.patch.object(QMessageBox, "exec", recording_exec).start()
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()

    def _widget(self, names):
        from core import log_recording
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        # 開いたままの案内を、後のテストへ残さない（w.close より先に閉じる）
        self.addCleanup(lambda: [box.accept() for box in _open_notices(w)])
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
        w.resize(800, 500)
        w.show()
        for name in names:
            self.addCleanup(log_recording.stop, name)
            w.create_terminal_tab(name)
        return w

    def _start(self, w, name, filename, release=None):
        """記録を始める。release を渡せば詰まる記録先、無ければ遅いが応答する記録先。"""
        path = os.path.join(self.dir, filename)
        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if isinstance(file, str) and file == path and "w" in mode:
                if release is None:
                    return _SlowFile(f, self.SLOW)
                return _StallingFile(f, release, 30.0)
            return f

        w.tab_widget.setCurrentWidget(w._terminals[name])
        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn(name, w._log_files, "前提: 記録が始まっている")
        return path

    def _stalled(self):
        release = threading.Event()
        self.releases.append(release)
        return release

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def _feed(self, w, gate, first, holds_wanted, seconds):
        """受信スレッドの代わり: 関所が開いている間だけ受信を渡し、止めた回数を数える。

        holds_wanted 回止めたら（止めたところで）戻る。渡し終えた行の次の番号と、
        止めた回数を返す。
        """
        fed = first
        holds = 0
        was_held = "dev" in w._log_throttled
        end = time.perf_counter() + seconds
        while holds < holds_wanted and time.perf_counter() < end:
            if gate.is_set():
                w.queue_output("dev", _chunk(fed, self.LINES))
                fed += self.LINES
            _pump(10)
            held = "dev" in w._log_throttled
            if held and not was_held:
                holds += 1
            was_held = held
        return fed, holds

    def _caught_up(self, w, gate):
        return (gate.is_set() and "dev" not in w._log_throttled
                and not w._pending_output.get("dev")
                and w._log_backlog("dev") == 0)

    def _stop_and_read(self, w, gate, path):
        """記録を止め、書き終えて閉じるのを待って中身を返す。"""
        from core import log_recording
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: self._caught_up(w, gate)
            and log_recording.device_using(path) is None, 20.0),
            "記録を停止しても、描き切って書き終えなかった")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_a_slow_store_gets_one_notice_per_recording(self):
        """遅いが応答する記録先で止めては再開するのを繰り返しても、案内は 1 回の記録で一度だけ。始め直せばまた一度。"""
        w = self._widget(["dev"])
        watch = _NoticeWatch(w, close_after=0.2)
        self.addCleanup(watch.stop)
        gate = w.output_gate("dev")

        path1 = self._start(w, "dev", "dev1.log")
        fed1, holds = self._feed(w, gate, 0, 3, 20.0)
        self.assertGreaterEqual(holds, 3, "前提: 止めては再開するのを繰り返した")
        _pump(300)
        self.assertEqual(
            len(watch.seen), 1,
            "1 回の記録のうちに、止めるたびに案内を出し直した（%d 回止めて %d 回）"
            % (holds, len(watch.seen)))
        text1 = self._stop_and_read(w, gate, path1)

        # 記録を始め直したら、また一度だけ知らせる
        path2 = self._start(w, "dev", "dev2.log")
        fed2, holds = self._feed(w, gate, fed1, 2, 20.0)
        self.assertGreaterEqual(holds, 2, "前提: 始め直した記録でも止めては再開した")
        _pump(300)
        self.assertEqual(
            len(watch.seen), 2,
            "始め直した記録で止めたのに知らせない、または出し直した（案内は計 %d 回）"
            % len(watch.seen))
        self.assertEqual([entry["modal"] for entry in watch.seen], [False, False],
                         "案内がモーダルで出た")
        self.assertEqual(self.execs, [], "案内を exec で出した")
        self.assertEqual(self.warning.call_count, 0,
                         "関所のある接続に、シリアル向けの警告などを出した")

        # 記録と画面は欠けない
        text2 = self._stop_and_read(w, gate, path2)
        self.assertEqual(text1, _chunk(0, fed1).replace("\r\n", "\n"),
                         "1 回目の記録が欠けた・崩れた")
        self.assertEqual(text2, _chunk(fed1, fed2 - fed1).replace("\r\n", "\n"),
                         "2 回目の記録が欠けた・崩れた")
        self.assertIn("L%06d" % (fed2 - 1), w._terminals["dev"].toPlainText())

    def test_other_tabs_take_keys_while_the_notice_is_open(self):
        """案内はモーダルでなく（exec しない）、開いている間もほかのタブへ打てること。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        w = self._widget(["dev", "other"])
        other = w._terminals["other"]
        other.set_input_enabled(True)
        typed = []
        other.key_pressed.connect(typed.append)
        release = self._stalled()
        path = self._start(w, "dev", "dev.log", release)
        # 利用者は別のタブで作業している
        w.tab_widget.setCurrentWidget(other)
        w.activateWindow()
        other.setFocus()
        seen = {}

        def on_open(box):
            # 利用者の打鍵と同じく、窓へ届ける（モーダルが開いていれば遮られる）
            QTest.keyClick(w.windowHandle(), Qt.Key.Key_X)
            _pump(50)
            seen["typed"] = list(typed)
            seen["modal"] = QApplication.activeModalWidget() is not None
            seen["open"] = box.isVisible()
        watch = _NoticeWatch(w, close_after=1.0, on_open=on_open)
        self.addCleanup(watch.stop)

        gate = w.output_gate("dev")
        fed, holds = self._feed(w, gate, 0, 1, 4.0)
        self.assertEqual(holds, 1, "前提: 記録待ちが上限を超えて受信を止めた")
        self.assertTrue(self._wait_until(lambda: "typed" in seen, 3.0),
                        "記録先の詰まりで受信を止めたのに、何も知らせなかった")
        self.assertIn("dev", watch.seen[0]["text"])
        self.assertTrue(seen["open"], "前提: 打ったときは案内が開いていた")
        self.assertFalse(seen["modal"], "案内がモーダルで出た（開いている間、どのタブにも打てない）")
        self.assertEqual(seen["typed"], ["x"], "案内が開いている間、ほかのタブへ打てなかった")
        self.assertEqual(self.execs, [], "案内を exec で出した")

        release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate), 10.0),
                        "詰まりが解けても受信・描画を再開しなかった")
        text = self._stop_and_read(w, gate, path)
        self.assertEqual(text, _chunk(0, fed).replace("\r\n", "\n"),
                         "止めている間の記録が欠けた・崩れた")
        self.assertEqual(len(watch.seen), 1)
        self.assertEqual(self.warning.call_count, 0)

    def test_no_second_notice_while_the_first_is_still_open(self):
        """同じ機器の案内が開いている間は、記録を始め直してまた止めても出し直さない。閉じたら、止めている新しい記録に一度出す。"""
        w = self._widget(["dev"])
        watch = _NoticeWatch(w, close_after=None)   # 利用者は閉じずに席を外している
        self.addCleanup(watch.stop)
        gate = w.output_gate("dev")

        first = self._stalled()
        path1 = self._start(w, "dev", "dev1.log", first)
        fed1, holds = self._feed(w, gate, 0, 1, 4.0)
        self.assertEqual(holds, 1, "前提: 記録待ちが上限を超えて受信を止めた")
        self.assertTrue(self._wait_until(lambda: watch.seen, 2.0),
                        "記録先の詰まりで受信を止めたのに、何も知らせなかった")
        self.assertFalse(watch.seen[0]["modal"], "案内がモーダルで出た")

        # 記録を停止すると止めが解ける（記録先は詰まったまま）。別のファイルへ
        # 始め直して、また止まる
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and "dev" not in w._log_throttled, 3.0),
            "前提: 記録を停止したら止めが解けた")
        second = self._stalled()
        path2 = self._start(w, "dev", "dev2.log", second)
        fed2, holds = self._feed(w, gate, fed1, 1, 4.0)
        self.assertEqual(holds, 1, "前提: 始め直した記録でまた受信を止めた")
        _pump(500)
        self.assertEqual(len(watch.seen), 1, "同じ機器の案内が開いているのに、出し直した")
        self.assertEqual(len(watch.open_boxes()), 1, "前提: 最初の案内は開いたまま")

        # 閉じたら、止めている新しい記録について一度だけ知らせる
        watch.open_boxes()[0].accept()
        self.assertTrue(self._wait_until(lambda: len(watch.seen) == 2, 2.0),
                        "始め直した記録で止めているのに、知らせなかった")
        _pump(500)
        self.assertEqual(len(watch.seen), 2, "案内を繰り返した")
        self.assertEqual(self.execs, [], "案内を exec で出した")

        from core import log_recording
        first.set()
        second.set()
        text2 = self._stop_and_read(w, gate, path2)
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path1) is None, 5.0))
        with open(path1, encoding="utf-8") as f:
            self.assertEqual(f.read(), _chunk(0, fed1).replace("\r\n", "\n"),
                             "1 回目の記録が欠けた・崩れた")
        self.assertEqual(text2, _chunk(fed1, fed2 - fed1).replace("\r\n", "\n"),
                         "2 回目の記録が欠けた・崩れた")
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
