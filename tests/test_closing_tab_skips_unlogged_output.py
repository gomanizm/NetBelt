"""タブを閉じる・アプリを閉じるとき、記録に入らない描き待ちをパーサへ通さないことを検証する。

タブを閉じるときに描き待ちを記録へ書く処理（TerminalWidget._log_pending_on_close）が、
記録していないタブでも描き待ちを全部パーサへ通していた。書く先が無いので、
通した結果は捨てるだけになる。記録せずに色付きの 70 文字前後の行を queue_output で
溜めてから _close_tab すると、GUI が止まった（検査役の実測: 16MiB で 1.0 秒、
64MiB で 4.6 秒。混んだ環境で測り直すと 16MiB で 1.9 秒、64MiB で 8.5 秒。
この処理を入れる前は描き待ちを捨てるだけで 0 秒）。停止して書き終えていない
記録だけが残っているタブも、その記録の区間より後ろ（どこにも書かない分）まで
全部通していた。アプリを閉じるとき（finish_log_recordings）も同じ関数を使う。

直し方: 記録中（_log_files）なら、これまでどおり全部を通して書く。記録中でなく、
停止して書き終えていない記録（_closing_logs）も無ければ、パーサへ通さずに捨てる。
停止した記録だけが残っているなら、その区間の合計（sum(entry[2])）文字だけを
通して書き、残りは捨てる。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _line(i):
    """機器の出力に多い、色付きの 70 文字前後の行"""
    return ("\x1b[32mGi0/%d\x1b[0m  up  up  \x1b[33m192.0.2.%d\x1b[0m"
            "  description link-%06d\r\n" % (i % 48, i % 250, i))


class ClosingTabSkipsUnloggedOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-close-skip-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(self._discard, w)
        w.create_terminal_tab("dev")
        return w

    @staticmethod
    def _discard(w):
        # 描き待ちを持たせたまま次のテストへ行かない（タイマーで描き始める）
        w._output_timer.stop()
        w._pending_output.clear()
        w.close()

    def _start(self, w, name):
        path = os.path.join(self.dir, name)
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn("dev", w._log_files, "前提: 記録が始まっている")
        return path

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _spy_parser(w):
        """パーサへ通した文字数を数える"""
        parser = w._terminals["dev"]._parser
        feed = parser.feed
        fed = []

        def counting(text):
            fed.append(len(text))
            return feed(text)
        parser.feed = counting
        return fed

    @staticmethod
    def _queue_lines(w, first, count):
        text = "".join(_line(i) for i in range(first, first + count))
        w.queue_output("dev", text)
        w._output_timer.stop()
        return text

    @staticmethod
    def _logged(first, count):
        return "".join("Gi0/%d  up  up  192.0.2.%d  description link-%06d\n"
                       % (i % 48, i % 250, i)
                       for i in range(first, first + count))

    def _close(self, w):
        w._close_tab(w.tab_widget.indexOf(w._terminals["dev"]))

    def test_closing_an_unrecorded_tab_does_not_parse_the_queue(self):
        """記録していないタブを閉じるとき、描き待ちをパーサへ通さないこと。"""
        w = self._widget()
        self._queue_lines(w, 0, 500)
        fed = self._spy_parser(w)

        self._close(w)

        self.assertEqual(sum(fed), 0,
                         "記録していないタブの描き待ち %d 文字をパーサへ通した"
                         % sum(fed))
        self.assertNotIn("dev", w._pending_output)

    def test_closing_an_unrecorded_tab_with_a_large_queue_does_not_freeze(self):
        """記録していないタブを、16MiB の描き待ちを溜めたまま閉じても止まらないこと。"""
        w = self._widget()
        chunk = "".join(_line(i) for i in range(64))
        for _ in range(16 * 1024 * 1024 // len(chunk) + 1):
            w.queue_output("dev", chunk)
        w._output_timer.stop()

        started = time.perf_counter()
        self._close(w)
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.5,
                        "16MiB の描き待ちがあるタブを閉じるのに %.2f 秒止まった"
                        % elapsed)

    def test_closing_a_tab_parses_only_what_a_stopped_recording_still_needs(self):
        """停止して書き終えていない記録だけが残るタブは、その区間だけを通して書くこと。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w, "a.log")
        handle = w._log_files["dev"]
        stopped = self._queue_lines(w, 0, 40)
        w.stop_log_recording("dev")
        self._queue_lines(w, 40, 400)       # 停止の後に受信した分（記録しない）
        fed = self._spy_parser(w)

        self._close(w)

        self.assertEqual(self._read(path), self._logged(0, 40),
                         "停止前に受信した分が記録に入らなかった")
        self.assertTrue(handle.closed, "書き終えたファイルを閉じていない")
        self.assertIsNone(log_recording.device_using(path))
        self.assertEqual(sum(fed), len(stopped),
                         "記録に入らない分までパーサへ通した（%d 文字。要るのは %d 文字）"
                         % (sum(fed), len(stopped)))

    def test_finishing_recordings_parses_only_what_a_stopped_recording_still_needs(self):
        """アプリを閉じるときも、停止した記録の区間だけを通して書くこと。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w, "b.log")
        handle = w._log_files["dev"]
        stopped = self._queue_lines(w, 0, 40)
        w.stop_log_recording("dev")
        self._queue_lines(w, 40, 400)
        fed = self._spy_parser(w)

        w.finish_log_recordings()

        self.assertEqual(self._read(path), self._logged(0, 40),
                         "停止前に受信した分が記録に入らなかった")
        self.assertTrue(handle.closed, "書き終えたファイルを閉じていない")
        self.assertIsNone(log_recording.device_using(path))
        self.assertNotIn("dev", w._closing_logs)
        self.assertEqual(sum(fed), len(stopped),
                         "記録に入らない分までパーサへ通した（%d 文字。要るのは %d 文字）"
                         % (sum(fed), len(stopped)))

    def test_closing_a_recording_tab_still_logs_every_queued_line(self):
        """記録中のタブは、停止した記録の区間も記録中の区間も全部書いてから閉じること。"""
        w = self._widget()
        first = self._start(w, "c1.log")
        self._queue_lines(w, 0, 30)
        w.stop_log_recording("dev")
        second = self._start(w, "c2.log")
        self._queue_lines(w, 30, 50)

        self._close(w)

        self.assertEqual(self._read(first), self._logged(0, 30))
        self.assertEqual(self._read(second), self._logged(30, 50))


if __name__ == "__main__":
    unittest.main()
