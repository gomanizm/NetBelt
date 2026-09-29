"""関所のある接続（SSH / Telnet）で受信を止めたことを、窓を出さずに知らせ、止めている間もフォーカスのあるタブへ打てることを検証する（5 周目 term の検査役の指摘と、6 周目の検査役の指摘）。

何が起きていたか（2ebc5b6 で実測。scratchpad\\cx132f-term\\logs の base_mine_cycle.txt と
base_mw_slow.txt。上限 256 KiB / 64 KiB）: 受信を止めたことの案内は
QMessageBox.information（exec のモーダル）で出ていた。一度だけ出すかを戻す条件
（書き込み待ちが PENDING_LOW_WATER まで減ったら _log_lag_noticed から外す）は、
関所のある接続では、止めて書き込み待ちが減るたびに満たされる。遅いが応答する
記録先では止めては再開するのを繰り返すので、止めるたびに案内が出直した
（1 回の書き込みに 0.15 秒かかる記録先で、20 秒に 3 回止めて案内も 3 回。
MainWindow と 127.0.0.1 の Telnet では、12 秒に 6 回止めて本物のモーダルが 6 回）。
開いている間は、モーダルがほかの窓への入力を遮るので、どのタブにも打てない。

5 周目では、案内をモーダルでない QMessageBox の show() で 1 回の記録に一度だけ
出すようにした。6 周目の検査役の実測（31b45ee。scratchpad\\cx132g-term\\logs の
base_focus_tab_other.txt）で、モーダルでなくても、出た瞬間に窓を活性化して
フォーカスを OK ボタンへ移すことが分かった。打っている最中に出ると残りの文字が
捨てられ、Enter は案内を閉じるだけになる（tests/test_log_recording_io_stall_status_line.py）。

どう直したか（作り手の親の決定）: 窓は出さない。止めている間は、記録中ダイアログ
（LogRecordingDialog）の状態の行に出し、再開したら消す。止めては再開するのを
繰り返しても、行が出て消えるだけ。端末の中に書く案内（show_notice）にはしない
（描き待ちを先に描き切るので、止めている意味が無くなる）。

窓は、表に出ている QMessageBox を見張って見つける（モーダルで出ても、モーダルで
なく出ても見つかる）。見つけたら、決めた時間がたったら閉じる（直す前の作りで
テストが止まったままにならないように）。キーは、本物のキーボードと同じく、
そのときフォーカスのあるウィジェットへ送る。上限はインスタンスで小さくする
（128 KiB / 32 KiB）。
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


def _open_boxes():
    """いま表に出ている QMessageBox"""
    from PyQt6.QtWidgets import QApplication, QMessageBox
    return [widget for widget in QApplication.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()]


class _WindowWatch:
    """表に出た QMessageBox を見張って記録し、close_after 秒たったら閉じる。

    モーダルの入れ子のループの中でもタイマーは動くので見つかる。見つけた窓には
    番号を付ける（閉じて消えた窓と取り違えない）。
    """

    def __init__(self, close_after=0.5):
        from PyQt6.QtCore import QTimer
        self.close_after = close_after
        self.mark = "netbelt_test_window_%d" % id(self)
        self.seen = []          # [{"text", "at", "closed"}]
        self._timer = QTimer()
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._scan)
        self._timer.start()

    def stop(self):
        self._timer.stop()
        for box in _open_boxes():
            box.accept()

    def _scan(self):
        now = time.perf_counter()
        for box in _open_boxes():
            index = box.property(self.mark)
            if index is None:
                self.seen.append({"text": box.text(), "at": now, "closed": False})
                box.setProperty(self.mark, len(self.seen) - 1)
                continue
            entry = self.seen[index]
            if not entry["closed"] and now - entry["at"] >= self.close_after:
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
        from PyQt6.QtWidgets import QMessageBox
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-modeless-")
        # 窓を出す呼び出しは、開かずにすぐ戻して数える
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.information = mock.patch(
            "PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)
        self.execs = []

        def recording_exec(box, *args):
            self.execs.append(box.text())
            return QMessageBox.StandardButton.Ok.value
        mock.patch.object(QMessageBox, "exec", recording_exec).start()
        self.watch = _WindowWatch()
        self.addCleanup(self.watch.stop)
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()

    def _widget(self, names):
        from core import log_recording
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
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

    @staticmethod
    def _held_line(w):
        """dev の記録中ダイアログに出ている『受信を止めて』の行（出ていなければ None）"""
        dialog = w._log_dialogs.get("dev")
        label = getattr(dialog, "status_label", None)
        if label is None or not label.isVisibleTo(dialog):
            return None
        return label.text() if HOLD_TEXT in label.text() else None

    def _feed(self, w, gate, first, holds_wanted, seconds, lines=None):
        """受信スレッドの代わり: 関所が開いている間だけ受信を渡し、止めた回数を数える。

        holds_wanted 回止めたら（止めたところで）戻る。渡し終えた行の次の番号と、
        止めた回数を返す。止めている間に状態の行が出たかを lines（リスト）へ足す。
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
            if lines is not None and self._held_line(w):
                lines.append(self._held_line(w))
        return fed, holds

    def _caught_up(self, w, gate):
        return (gate.is_set() and "dev" not in w._log_throttled
                and not w._pending_output.get("dev")
                and w._log_backlog("dev") == 0)

    def _assert_no_window(self):
        self.assertEqual(self.watch.seen, [], "記録先の詰まりを知らせるのに窓を出した")
        self.assertEqual(self.execs, [], "窓を exec で出した")
        self.assertEqual(self.information.call_count, 0, "モーダルの窓を出した")
        self.assertEqual(self.warning.call_count, 0,
                         "関所のある接続に、シリアル向けの警告などを出した")

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

    def test_a_slow_store_holding_again_and_again_opens_no_window(self):
        """遅いが応答する記録先で止めては再開するのを繰り返しても、窓は 1 つも出さず、状態の行で知らせること。始め直した記録でも同じ。"""
        w = self._widget(["dev"])
        gate = w.output_gate("dev")

        path1 = self._start(w, "dev", "dev1.log")
        lines = []
        fed1, holds = self._feed(w, gate, 0, 3, 20.0, lines)
        self.assertGreaterEqual(holds, 3, "前提: 止めては再開するのを繰り返した")
        _pump(300)
        self._assert_no_window()
        self.assertTrue(lines, "%d 回止めたのに、状態の行を一度も出さなかった" % holds)
        text1 = self._stop_and_read(w, gate, path1)

        # 記録を始め直しても同じ
        path2 = self._start(w, "dev", "dev2.log")
        lines = []
        fed2, holds = self._feed(w, gate, fed1, 2, 20.0, lines)
        self.assertGreaterEqual(holds, 2, "前提: 始め直した記録でも止めては再開した")
        _pump(300)
        self._assert_no_window()
        self.assertTrue(lines, "始め直した記録で止めたのに、状態の行を出さなかった")

        # 記録と画面は欠けない
        text2 = self._stop_and_read(w, gate, path2)
        self.assertEqual(text1, _chunk(0, fed1).replace("\r\n", "\n"),
                         "1 回目の記録が欠けた・崩れた")
        self.assertEqual(text2, _chunk(fed1, fed2 - fed1).replace("\r\n", "\n"),
                         "2 回目の記録が欠けた・崩れた")
        self.assertIn("L%06d" % (fed2 - 1), w._terminals["dev"].toPlainText())

    def test_other_tabs_take_keys_while_held(self):
        """止めている間も、フォーカスのあるほかのタブへ打てること（窓がキーを奪わない。Enter も届く）。"""
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
        _pump(50)

        gate = w.output_gate("dev")
        fed, holds = self._feed(w, gate, 0, 1, 4.0)
        self.assertEqual(holds, 1, "前提: 記録待ちが上限を超えて受信を止めた")
        self.assertTrue(self._wait_until(
            lambda: self._held_line(w) or self.watch.seen, 3.0),
            "記録先の詰まりで受信を止めたのに、何も知らせなかった")
        # 本物のキーボードと同じく、いまフォーカスのあるウィジェットへ打つ
        went = []
        for key in (Qt.Key.Key_X, Qt.Key.Key_Return):
            target = QApplication.focusWidget() or QApplication.activeWindow()
            went.append(type(target).__name__)
            QTest.keyClick(target, key)
            _pump(20)
        self.assertEqual(typed, ["x", "\r"],
                         "止めている間、ほかのタブへ打てなかった（キーの行き先: %r）" % went)
        self.assertIs(QApplication.focusWidget(), other, "端末からフォーカスを移した")
        self._assert_no_window()

        release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate), 10.0),
                        "詰まりが解けても受信・描画を再開しなかった")
        text = self._stop_and_read(w, gate, path)
        self.assertEqual(text, _chunk(0, fed).replace("\r\n", "\n"),
                         "止めている間の記録が欠けた・崩れた")
        self._assert_no_window()

    def test_a_restarted_recording_shows_the_line_in_its_own_dialog(self):
        """記録を停止して（停止した記録は詰まったまま）始め直し、また止めたら、新しい記録中ダイアログに出すこと。窓は出さないこと。"""
        w = self._widget(["dev"])
        gate = w.output_gate("dev")

        first = self._stalled()
        path1 = self._start(w, "dev", "dev1.log", first)
        dialog1 = w._log_dialogs["dev"]
        fed1, holds = self._feed(w, gate, 0, 1, 4.0)
        self.assertEqual(holds, 1, "前提: 記録待ちが上限を超えて受信を止めた")
        self.assertTrue(self._wait_until(lambda: self._held_line(w), 2.0),
                        "記録先の詰まりで受信を止めたのに、何も知らせなかった")

        # 記録を停止すると止めが解ける（記録先は詰まったまま）。別のファイルへ
        # 始め直して、また止まる
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and "dev" not in w._log_throttled, 3.0),
            "前提: 記録を停止したら止めが解けた")
        second = self._stalled()
        path2 = self._start(w, "dev", "dev2.log", second)
        self.assertIsNot(w._log_dialogs["dev"], dialog1, "前提: 新しい記録中ダイアログ")
        self.assertIsNone(self._held_line(w), "始め直した記録に、前の記録の行を出した")
        fed2, holds = self._feed(w, gate, fed1, 1, 4.0)
        self.assertEqual(holds, 1, "前提: 始め直した記録でまた受信を止めた")
        self.assertTrue(self._wait_until(lambda: self._held_line(w), 2.0),
                        "始め直した記録で止めたのに、知らせなかった")
        _pump(500)
        self.assertTrue(self._held_line(w), "止めている間に状態の行を消した")
        self._assert_no_window()

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
        self._assert_no_window()


if __name__ == "__main__":
    unittest.main()
