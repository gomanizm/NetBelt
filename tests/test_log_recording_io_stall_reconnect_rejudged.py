"""記録先の詰まりで止めた受信を、再接続した次の接続へ引き継がないことを検証する（4 周目 term の (4)）。

何が起きていたか（b2858c4 で実測。scratchpad\\cx132e-term\\probe_hold.py の
reconnect と、このテストの再現。上限 256 KiB / 64 KiB）: 記録待ちが上限を超えると、
関所のある接続（SSH / Telnet）はその機器を _log_throttled に入れ、描画を止めて
受信の関所を閉じる。関所は機器名で持ち回され、MainWindow._attach_read_gate は
同じ Event を次の接続へ渡す（tests/test_terminal_reconnect_draw_keeps_gate.py）。
記録を止めずに再接続すると、描き待ちは前の画面へ描き切られるが、
throttled=True・関所は閉じたままが次の接続へ引き継がれた。再開は書き込み待ちが
PENDING_LOW_WATER（64 KiB）まで減ったときだけなので、記録先が応答し始めて
書き込み待ちが上限を下回っていても（再接続の時点で 181,248 文字。上限は
262,144 文字）、再接続した先の関所は 1 秒たっても閉じたままで、次の接続は
1 文字も読めず、プロンプトが出なかった。

どう直したか（作り手の親の決定）: 再接続（create_terminal_tab の既存タブの分岐）
で前の画面へ描き切ったあと、その機器の止め（_log_throttled）を外して関所を
開け直す。前の接続の止めは引き継がず、記録がまだ詰まっていれば、次の接続の
受信で改めて判定する（上限を超えていれば、また止める）。記録と画面を欠かさない
性質は保つ。

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


class _MeteredFile:
    """write を、許しが出た回数だけ通す記録ファイル（応答の遅い共有フォルダの代わり）。

    permit(n) で n 回分を通し、open_all() で以後は全部通す。許しが出ないまま
    limit 秒たったら通す（直す前の作りでテストが止まったままにならないように）。
    """

    def __init__(self, f, limit):
        self._f = f
        self.name = f.name
        self._limit = limit
        self._permits = threading.Semaphore(0)
        self._all = threading.Event()

    def permit(self, count):
        for _ in range(count):
            self._permits.release()

    def open_all(self):
        self._all.set()
        self.permit(100000)

    def write(self, text):
        if not self._all.is_set():
            self._permits.acquire(timeout=self._limit)
        return self._f.write(text)

    def flush(self):
        return self._f.flush()

    def close(self):
        self._all.wait(self._limit)
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


def _chunk(first, count, tag="L"):
    return "".join("%s%06d %s\r\n" % (tag, i, "y" * 50)
                   for i in range(first, first + count))


class LogRecordingIoStallReconnectRejudgedTest(unittest.TestCase):
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
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-reconnect-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.information = mock.patch(
            "PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)
        self.files = []
        self.addCleanup(lambda: [f.open_all() for f in self.files])

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
                f = _MeteredFile(f, 30.0)
                self.files.append(f)
            return f

        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn("dev", w._log_files, "前提: 記録が始まっている")
        return path, self.files[-1]

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def _feed_while_open(self, w, gate, first, seconds, tag="L", stop_when_held=True):
        """受信スレッドの代わり: 関所が開いている間だけ受信を渡す。"""
        fed = first
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            if gate.is_set():
                w.queue_output("dev", _chunk(fed, self.LINES, tag))
                fed += self.LINES
            _pump(20)
            if stop_when_held and not gate.is_set() and "dev" in w._log_throttled:
                break
        return fed

    def _hold(self, w, gate):
        fed = self._feed_while_open(w, gate, 0, 4.0)
        self.assertFalse(gate.is_set(), "前提: 記録待ちが上限を超えて受信を止めた")
        self.assertIn("dev", w._log_throttled, "前提: この機器の描画を止めている")
        return fed

    def _hold_notices(self):
        return [c for c in self.information.call_args_list
                if len(c.args) > 2 and "受信を止めて" in str(c.args[2])]

    def test_a_reconnect_does_not_carry_over_the_hold_once_below_the_limit(self):
        """記録待ちが上限を下回ってから再接続したら、次の接続は止めずに読めて描かれること。"""
        from core import log_recording
        w = self._widget()
        path, logfile = self._start(w)
        gate = w.output_gate("dev")
        fed = self._hold(w, gate)

        # 記録先が少し応答して、書き込み待ちが上限と再開の水位の間まで減った。
        # 1 回ずつ通し、書き終えて減ったのを見てから次を通す（通しすぎない）
        writer = w._log_files["dev"]
        target = (self.HIGH + self.LOW) // 2
        end = time.perf_counter() + 5.0
        while writer.backlog > target and time.perf_counter() < end:
            before = writer.backlog
            logfile.permit(1)
            self._wait_until(lambda: writer.backlog < before, 2.0)
        self.assertTrue(self.LOW < writer.backlog <= target,
                        "前提: 書き込み待ちが上限と再開の水位の間にある（%d）"
                        % writer.backlog)
        _pump(300)
        self.assertIn("dev", w._log_throttled,
                      "前提: 同じ接続のままなら、再開の水位までは止めたまま")

        w.create_terminal_tab("dev")            # 記録は止めずに再接続
        self.assertIn("L%06d" % (fed - 1), w._terminals["dev"].toPlainText(),
                      "前提: 前の接続の描き待ちは前の画面へ描き切った")
        self.assertTrue(self._wait_until(lambda: gate.is_set(), 1.0),
                        "前の接続で止めた受信を、再接続した次の接続へ引き継いだ"
                        "（書き込み待ち %d 文字は上限 %d を下回っている）"
                        % (writer.backlog, self.HIGH))
        # 次の接続のログイン後の出力（プロンプトまで。上限を超えない量）
        after = 64
        w.queue_output("dev", _chunk(0, after, "N"))
        last = "N%06d" % (after - 1)
        self.assertTrue(self._wait_until(
            lambda: last in w._terminals["dev"].toPlainText(), 3.0),
            "再接続した次の接続の受信が描かれない")

        logfile.open_all()
        self.assertTrue(self._wait_until(
            lambda: w._log_backlog("dev") == 0, 5.0), "前提: 書き終えた")
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(
                f.read(), (_chunk(0, fed) + _chunk(0, after, "N")).replace("\r\n", "\n"),
                "再接続の前後の記録が欠けた・崩れた")
        self.assertEqual(self.warning.call_count, 0)

    def test_a_reconnect_while_still_over_the_limit_holds_the_new_link_again(self):
        """記録待ちが上限を超えたままなら、次の接続の受信で改めて止め、解けたら欠けなく描くこと。"""
        from core import log_recording
        w = self._widget()
        path, logfile = self._start(w)
        gate = w.output_gate("dev")
        fed = self._hold(w, gate)
        self.assertTrue(self._wait_until(lambda: self._hold_notices(), 2.0),
                        "前提: 止めたことを知らせた")

        w.create_terminal_tab("dev")            # 記録は止めずに再接続
        after = self._feed_while_open(w, gate, 0, 1.0, tag="N")
        _pump(300)
        self.assertFalse(gate.is_set(), "記録がまだ詰まっているのに、次の接続の受信を止めない")
        self.assertIn("dev", w._log_throttled)
        self.assertEqual(len(self._hold_notices()), 1,
                         "同じ詰まりのうちに、止めたことの知らせを繰り返した")

        logfile.open_all()
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and not w._pending_output.get("dev")
            and w._log_backlog("dev") == 0, 10.0),
            "詰まりが解けても受信・描画を再開しなかった")
        if after:
            self.assertIn("N%06d" % (after - 1), w._terminals["dev"].toPlainText())
        w.stop_log_recording("dev")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(
                f.read(), (_chunk(0, fed) + _chunk(0, after, "N")).replace("\r\n", "\n"),
                "再接続の前後の記録が欠けた・崩れた")
        self.assertEqual(self.warning.call_count, 0)


if __name__ == "__main__":
    unittest.main()
