"""停止した記録への書き込みが失敗しても、記録中のファイルの中身の順序が崩れないことを検証する。

描き待ちから取り出した 1 片は、停止して書き終えていない記録の区間と、
記録中のファイルの区間に分けて書く（TerminalWidget._write_logs）。停止した記録への
書き込みが失敗すると、同じ片の残り（記録中のファイルの分）を書く前に
QMessageBox.warning（モーダル）を出していた。警告が出ている間もイベントループは
回るので、描画のタイマーが次の片を描いて記録中のファイルへ先に書く。警告を
閉じると前の片の残りがその後ろに書かれ、記録中のファイルの中身の順序が入れ替わる
（このテストの手順で、記録中のファイルが 10 行目からではなく、次の片の頭にあたる
268 行目の途中から始まり、10〜268 行目はその後ろに書かれた）。

直し方: for の中では失敗した記録を控える（そのハンドルは以後使わない）だけにし、
閉じて警告を出す（_close_stopped_log に error を渡す）のは、同じ片を全部書き終えて、
書き終えた停止記録の後始末も済ませてからにする。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _line(i):
    return "\x1b[36mline %05d\x1b[0m  abcdefghijklmnopqrstuvwxyz  0123456789\r\n" % i


def _logged(first, end):
    return "".join("line %05d  abcdefghijklmnopqrstuvwxyz  0123456789\n" % i
                   for i in range(first, end))


class _FailingHandle:
    """書き込むと必ず失敗する記録ファイル（ディスク満杯・共有フォルダの切断の代わり）"""

    def __init__(self, name):
        self.name = name
        self.closed = False

    def write(self, text):
        raise OSError(28, "No space left on device")

    def flush(self):
        pass

    def close(self):
        self.closed = True


class StoppedLogFailureKeepsLogOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-stopped-fail-")
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

    def _drain(self, w):
        while w._pending_output:
            w._flush_pending_output()
        w._output_timer.stop()

    def test_a_failed_stopped_log_does_not_reorder_the_running_log(self):
        """停止した記録の失敗の警告の間に次の片が描かれても、記録中のファイルの順序が保たれること。"""
        from core import log_recording
        w = self._widget()
        first = self._start(w, "old.log")
        w.queue_output("dev", "".join(_line(i) for i in range(10)))
        w.stop_log_recording("dev")
        [entry] = w._closing_logs["dev"]
        real = entry[0]
        self.addCleanup(real.close)
        failing = _FailingHandle(first)
        entry[0] = failing                  # 停止した記録への書き込みが失敗する

        second = self._start(w, "new.log")
        w.queue_output("dev", "".join(_line(i) for i in range(10, 400)))
        self.assertGreater(len(w._pending_output["dev"]), w.OUTPUT_SLICE,
                           "前提: 描き待ちが 2 片以上ある")

        seen = []

        def modal_runs_the_event_loop(*args):
            # 警告が出ている間にイベントループが回り、描画のタイマーが次の片を描く
            seen.append((args[2], self._read(second)))
            if len(seen) == 1:
                w._flush_pending_output()
        self.warning.side_effect = modal_runs_the_event_loop

        self._drain(w)

        self.assertEqual(len(seen), 1, "警告の回数が違う: %r" % [s[0] for s in seen])
        message, logged_at_warning = seen[0]
        self.assertIn("書き終えられませんでした", message)
        # 1 片目は停止した記録の 10 行と、記録中のファイルの先頭の分でできている
        self.assertTrue(logged_at_warning and
                        _logged(10, 400).startswith(logged_at_warning),
                        "警告を出した時点で、同じ片の記録中のファイルの分を"
                        "書き終えていない: %r" % logged_at_warning[:80])
        self.assertEqual(self._read(second), _logged(10, 400),
                         "記録中のファイルの中身の順序が入れ替わった")
        self.assertTrue(failing.closed, "失敗した停止記録を閉じていない")
        self.assertIsNone(log_recording.device_using(first),
                          "失敗した停止記録が使用中のまま残った")
        self.assertNotIn("dev", w._closing_logs)
        self.assertIn("dev", w._log_files, "記録中の記録まで止まった")
        w.stop_log_recording("dev")


if __name__ == "__main__":
    unittest.main()
