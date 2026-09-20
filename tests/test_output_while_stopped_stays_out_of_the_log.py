"""記録していなかった間の受信が、後から始めた記録へ混じらないことを検証する。

受信は queue_output で溜めてから描き、記録へ書くのは描いたとき。停止側
（_stop_log_recording_for）は「停止より前に受信した分」の文字数を _closing_logs
へ預けるので、その区間は止めたファイルへ入る。ところが開始側
（start_log_recording）は「記録していなかった区間」を何も取り置かないため、
_split_for_logs が停止ログの区間を配ったあと、残り全部 — 停止中に受信した分も
含めて — が記録中のファイルへ回っていた。

大量出力で描き待ちが残っている最中に「停止 → 再開」すると、記録から外した
かった区間がそのまま次のファイルの先頭に入る。

実測（基準 16101ef、offscreen、実機なし。start/stop は実経路、QFileDialog だけ
mock）: A で記録開始 → 'ON-WHILE-A' を受信（描き待ち）→ 停止 → 停止中に
'OFF-WHILE-STOPPED' を受信 → B で記録開始 → 'ON-WHILE-B' を受信 → 描き待ちを
排出、で

    A.log = 'ON-WHILE-A\\n'
    B.log = 'OFF-WHILE-STOPPED\\nON-WHILE-B\\n'   ← 停止中の分が入っている

直し方: start_log_recording で _log_files を立てる直前に、停止側と同じ式で
「描き待ちのうち、どの _closing_logs 区間にも割り当てられていない文字数」を
求め、ハンドルを持たない「捨てる区間」として _closing_logs へ積む。
_split_for_logs / _write_logs はハンドルが None の区間を読み飛ばす仕組みを
既に持っているので、そこへ乗せる。記録を始める前に受信していた分も同じ式で
外れる（記録は「開始より後に受信した分」だけになる）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class OutputWhileStoppedStaysOutOfTheLogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-stopped-gap-")
        # offscreen では通知を閉じる相手がいない（出たら失敗として見る）
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(self._discard, w)
        w.create_terminal_tab("dev")
        w.tab_widget.setCurrentIndex(w.tab_widget.count() - 1)
        self.assertEqual(w.get_current_tab_name(), "dev", "前提: dev を表示")
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
        """溜まっている受信を、イベントループへ戻る回と同じ手順で描き切る。"""
        while w._pending_output:
            w._flush_pending_output()
        w._output_timer.stop()

    def test_output_received_while_stopped_is_not_in_the_next_file(self):
        """停止中に受信した分が、再開して始めたファイルに入らないこと。"""
        w = self._widget()
        first = self._start(w, "a.log")
        w.queue_output("dev", "ON-WHILE-A\r\n")
        w.stop_log_recording("dev")
        w.queue_output("dev", "OFF-WHILE-STOPPED\r\n")
        second = self._start(w, "b.log")
        w.queue_output("dev", "ON-WHILE-B\r\n")

        self._drain(w)

        self.assertEqual(self._read(first), "ON-WHILE-A\n",
                         "止めたファイルの中身が変わった")
        self.assertEqual(self._read(second), "ON-WHILE-B\n",
                         "記録していなかった間の受信が次のファイルに入った")
        self.assertIn("OFF-WHILE-STOPPED", w._terminals["dev"].toPlainText(),
                      "画面には出ているべき（記録から外すだけ）")
        w.stop_log_recording("dev")
        self.warning.assert_not_called()

    def test_closing_the_tab_does_not_log_what_arrived_while_stopped(self):
        """タブを閉じる経路でも、停止中の受信が記録へ入らないこと。"""
        w = self._widget()
        first = self._start(w, "c.log")
        w.queue_output("dev", "ON-WHILE-A\r\n")
        w.stop_log_recording("dev")
        w.queue_output("dev", "OFF-WHILE-STOPPED\r\n")
        second = self._start(w, "d.log")
        w.queue_output("dev", "ON-WHILE-B\r\n")

        w._close_tab(w.tab_widget.indexOf(w._terminals["dev"]))

        self.assertEqual(self._read(first), "ON-WHILE-A\n")
        self.assertEqual(self._read(second), "ON-WHILE-B\n",
                         "閉じるときに、停止中の受信まで記録へ書いた")
        self.warning.assert_not_called()

    def test_output_received_before_the_first_start_is_not_recorded(self):
        """記録を始める前に受信して、まだ描いていない分も記録に入らないこと。"""
        w = self._widget()
        w.queue_output("dev", "BEFORE-START\r\n")
        path = self._start(w, "e.log")
        w.queue_output("dev", "AFTER-START\r\n")

        self._drain(w)

        self.assertEqual(self._read(path), "AFTER-START\n",
                         "記録を始める前の受信が記録に入った")
        w.stop_log_recording("dev")
        self.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
