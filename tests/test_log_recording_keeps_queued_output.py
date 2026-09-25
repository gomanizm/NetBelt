"""ログ記録を止めたとき、停止より前に受信してまだ描いていない出力も記録へ入ることを検証する。

受信は queue_output で溜めてから描き、ログへの書き込みは描いた後に行う。
記録の停止は溜まり分に触れずにハンドルを閉じ、タブを閉じるときは溜まり分を
捨てていた。受信が描画を上回って溜まっている最中に止めると:

  - 停止前に受信した分が、画面には出るのに記録に入らない
  - 停止の直後に別のファイルで記録を始めると、その分が新しいファイルに入る
  - タブを閉じると、溜まっていた末尾が記録に入らない

実測（検証役）: 3,000,000 文字（60,000 行）を 4096 文字ずつ queue_output し、
0.5 秒イベントループを回してから記録中ダイアログの「記録停止」を押すと、
停止の時点で 2,361,024 文字が描かれておらず、記録は 12,780 行だけだった。
ダイアログには「受信したデータがリアルタイムで保存されます」とある。

停止した時点の溜まり量を境目として覚え、描き進んでその境目に届くまでは
ハンドルを閉じずにそのファイルへ書く（届いたら閉じ、使用中の登録も外す）。
新しい記録には境目より後ろだけが入る。タブを閉じるときは、捨てる前に
パーサへ通して記録にだけ書く。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class LogRecordingKeepsQueuedOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logqueue-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("dev")
        return w

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

    def test_output_received_before_the_stop_is_recorded(self):
        """停止より前に受信した分は、停止の後で描かれても記録に入ること。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w, "a.log")
        handle = w._log_files["dev"]
        w.append_output("dev", "FIRST\r\n")
        w.queue_output("dev", "LAST\r\n")

        w.stop_log_recording("dev")
        self.assertNotIn("dev", w._log_files, "停止が記録中のまま残った")
        self.assertNotIn("dev", w._log_dialogs, "記録中ダイアログが残った")
        self.assertEqual(log_recording.device_using(path), "dev",
                         "書き終える前のファイルが使用中から外れた")
        self._drain(w)

        self.assertIn("LAST", w._terminals["dev"].toPlainText(), "前提: 画面には出た")
        self.assertEqual(self._read(path), "FIRST\nLAST\n",
                         "停止前に受信した分が記録に入らなかった")
        self.assertTrue(handle.closed, "書き終えたのにファイルを閉じていない")
        self.assertIsNone(log_recording.device_using(path),
                          "書き終えたのに使用中のまま残った")

    def test_output_received_before_the_stop_stays_out_of_the_next_file(self):
        """停止の直後に始めた別の記録には、停止前に受信した分が入らないこと。"""
        from core import log_recording
        w = self._widget()
        first = self._start(w, "b1.log")
        w.queue_output("dev", "OLDSESSION\r\nOLD\x1b[3")
        w.stop_log_recording("dev")
        second = self._start(w, "b2.log")
        w.queue_output("dev", "1mNEW\x1b[0m\r\n")
        self._drain(w)

        self.assertEqual(self._read(first), "OLDSESSION\nOLD",
                         "停止前に受信した分が前のファイルに入っていない")
        self.assertEqual(self._read(second), "NEW\n",
                         "停止前に受信した分が次のファイルに入った")
        self.assertEqual(log_recording.device_using(second), "dev",
                         "前の記録を閉じたときに、次の記録の登録まで外した")
        w.stop_log_recording("dev")

    def test_each_stopped_recording_gets_what_arrived_while_it_was_on(self):
        """止めては始めるを繰り返しても、それぞれの区間の受信がそのファイルへ入ること。"""
        w = self._widget()
        first = self._start(w, "c1.log")
        w.queue_output("dev", "one\r\n")
        w.stop_log_recording("dev")
        second = self._start(w, "c2.log")
        w.queue_output("dev", "two\r\n")
        w.stop_log_recording("dev")
        w.queue_output("dev", "untracked\r\n")
        self._drain(w)

        self.assertEqual(self._read(first), "one\n")
        self.assertEqual(self._read(second), "two\n")

    def test_stopping_with_the_dialog_during_a_backlog_records_every_line(self):
        """大量受信の描画待ちの最中に「記録停止」を押しても、受信した行が全部入ること。"""
        from PyQt6.QtWidgets import QPushButton
        w = self._widget()
        path = self._start(w, "d.log")
        blob = "".join("line %06d abcdefghijklmnopqrstuvwxyz\r\n" % i
                       for i in range(3000))
        for i in range(0, len(blob), 4096):
            w.queue_output("dev", blob[i:i + 4096])
        w._flush_pending_output()           # 途中まで描いたところで止める
        self.assertTrue(w._pending_output, "前提: 描いていない分が残っている")

        dialog = w._log_dialogs["dev"]
        [button] = [b for b in dialog.findChildren(QPushButton)
                    if b.text() == "記録停止"]
        button.click()
        self._drain(w)

        logged = self._read(path).splitlines()
        self.assertEqual(len(logged), 3000,
                         "受信した行が記録から欠けた（%d / 3000 行）" % len(logged))
        self.assertEqual(logged, ["line %06d abcdefghijklmnopqrstuvwxyz" % i
                                  for i in range(3000)], "記録の中身が崩れた")

    def test_closing_the_tab_records_the_output_that_was_not_drawn(self):
        """タブを閉じても、溜まっていた受信の末尾が記録に入ること。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w, "e.log")
        handle = w._log_files["dev"]
        w.append_output("dev", "BEFORECLOSE\r\n")
        w.queue_output("dev", "TAIL\x1b[31mLINE\x1b[0m\r\n")

        index = w.tab_widget.indexOf(w._terminals["dev"])
        w._close_tab(index)

        self.assertEqual(self._read(path), "BEFORECLOSE\nTAILLINE\n",
                         "閉じたタブの溜まっていた受信が記録に入らなかった")
        self.assertTrue(handle.closed, "閉じたタブのファイルを閉じていない")
        self.assertIsNone(log_recording.device_using(path))

    def test_closing_the_tab_finishes_a_recording_that_was_still_being_written(self):
        """停止して書き終える前にタブを閉じても、停止前の受信が入り、ファイルが閉じること。"""
        w = self._widget()
        path = self._start(w, "f.log")
        handle = w._log_files["dev"]
        w.queue_output("dev", "PENDING\r\n")
        w.stop_log_recording("dev")

        w._close_tab(w.tab_widget.indexOf(w._terminals["dev"]))

        self.assertEqual(self._read(path), "PENDING\n")
        self.assertTrue(handle.closed, "書きかけのファイルが開いたまま残った")

    def test_stopping_without_a_backlog_closes_at_once(self):
        """溜まりが無ければ、これまでどおり停止した時点で閉じること。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w, "g.log")
        handle = w._log_files["dev"]
        w.append_output("dev", "DONE\r\n")

        w.stop_log_recording("dev")

        self.assertTrue(handle.closed, "溜まりが無いのに閉じるのを待っている")
        self.assertIsNone(log_recording.device_using(path))
        self.assertEqual(self._read(path), "DONE\n")


if __name__ == "__main__":
    unittest.main()
