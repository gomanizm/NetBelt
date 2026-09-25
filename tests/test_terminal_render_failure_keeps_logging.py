"""描画の途中で例外が出ても、記録と排出が続くことを検証する。

何が起きていたか（実測、基準 f4cad23）:
append_output はパーサへ通して画面を描いてから記録へ書いていたので、
_render_screen が例外を投げると _write_logs へ届かなかった。受信片は
_flush_pending_output が既に描き待ちから取り出した後なので、もう一度
記録へ回す道が無い。記録中に 16,380 文字（AAAA 1365 行）と 16,380 文字
（BBBB 1365 行）を queue_output し、_paint_row を 1 回だけ RuntimeError に
した実測では、画面に AAAA が 1365 行出ているのに記録ファイルは AAAA 0 行 /
BBBB 0 行だった。停止した記録でも同じで、_split_for_logs が描く前に
区間（entry[2]）を減らすため、その区間だけが書かれないまま消費済みになり、
停止ログは AAAA 0 行 / BBBB 1364 行（期待は各 1365 行）で欠落を知らせずに
閉じられた。
さらに _flush_pending_output は例外で末尾まで届かないので、受信の関所
（_update_output_gate）も単発タイマーの再始動も飛ばされた。実測では
timer active: False・関所は閉じたままで、残り 16,376 文字が永久に描かれず、
受信スレッドも読まないのでその機器の端末が恒久停止した。

どう直したか:
append_output の _render_screen を try/finally で囲み、finally で
_write_logs を必ず通してから例外を再送出する（_write_logs は受信事象だけを
見ていて画面状態に依存しない）。_flush_pending_output は機器ごとの本体を
try/finally にして _update_output_gate を finally へ移し、タイマーの再始動も
外側の finally へ置いた。取り出した片はパーサへ通し済みなので描き待ちへは
戻さない（戻すと二重描画になる）。例外そのものは握り潰さず再送出する。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 1 行 12 文字（"AAAA %05d\r\n"）で、OUTPUT_SLICE 16384 に収まる分を作る
LINE_LEN = len("AAAA %05d\r\n" % 0)


class RenderFailureKeepsLoggingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _discard(w):
        """描き残しを持ったまま次のテストへ行かない。

        描き残しがあると次のテストの最中にタイマーで描き始め、その途中で
        GC がこの widget を捨てるとプロセスごと落ちる
        （tests/test_window_close_finishes_log_recording.py の同名の注記）。
        """
        w._output_timer.stop()
        w._pending_output.clear()
        w.close()

    def _widget(self):
        from PyQt6.QtWidgets import QMessageBox
        from ui.terminal_widget import TerminalWidget
        for name in ("warning", "information", "critical"):
            patcher = mock.patch.object(
                QMessageBox, name,
                return_value=QMessageBox.StandardButton.Ok)
            patcher.start()
            self.addCleanup(patcher.stop)
        w = TerminalWidget()
        self.addCleanup(self._discard, w)
        w.resize(900, 500)
        w.show()
        w.create_terminal_tab("dev")
        self.app.processEvents()
        return w

    def _start_recording(self, w):
        """記録を始めて、その保存先を返す。"""
        from PyQt6.QtWidgets import QFileDialog
        tmpdir = tempfile.mkdtemp(prefix="termui01-")
        path = os.path.join(tmpdir, "dev.log")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(path, "")):
            w.start_log_recording()
        self.assertIn("dev", w._log_files, "前提: 記録が始まっていない")
        return path

    @staticmethod
    def _lines(tag, count):
        return "".join("%s %05d\r\n" % (tag, i) for i in range(count))

    def _failing_once(self, w):
        """_paint_row を 1 回だけ失敗させる（2 回目からは本物）。"""
        real = w._paint_row
        calls = []

        def paint(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("injected paint failure")
            return real(*args, **kwargs)

        return mock.patch.object(w, "_paint_row", side_effect=paint)

    def _flush_once(self, w):
        """タイマーが 1 回発火したのと同じ形で排出する。"""
        # 単発タイマーは発火した時点で止まっている。手で呼ぶときも揃える
        w._output_timer.stop()
        with self.assertRaises(RuntimeError):
            w._flush_pending_output()

    def test_a_failed_render_still_writes_the_piece_to_the_log(self):
        """描画が失敗しても、その受信片が記録ファイルへ書かれること。"""
        w = self._widget()
        path = self._start_recording(w)
        count = w.OUTPUT_SLICE // LINE_LEN
        w.queue_output("dev", self._lines("AAAA", count))
        w.queue_output("dev", self._lines("BBBB", count))

        with self._failing_once(w):
            self._flush_once(w)

        screen = w._terminals["dev"].toPlainText()
        self.assertEqual(screen.count("AAAA"), count,
                         "前提: 画面には AAAA が全部出ている")
        w._log_files["dev"].flush()
        body = open(path, encoding="utf-8").read()
        self.assertEqual(body.count("AAAA"), count,
                         "画面に出た受信が記録から消えている")

    def test_a_failed_render_keeps_the_gate_and_the_timer_going(self):
        """描画が失敗しても、受信の関所が開き直り、排出のタイマーが続くこと。"""
        w = self._widget()
        gate_patch = mock.patch.object(type(w), "PENDING_HIGH_WATER", 1000)
        gate_patch.start()
        self.addCleanup(gate_patch.stop)
        low_patch = mock.patch.object(type(w), "PENDING_LOW_WATER", 500)
        low_patch.start()
        self.addCleanup(low_patch.stop)

        gate = w.output_gate("dev")
        count = w.OUTPUT_SLICE // LINE_LEN
        w.queue_output("dev", self._lines("AAAA", count))
        w.queue_output("dev", self._lines("BBBB", 8))
        self.assertFalse(gate.is_set(), "前提: 溜まりすぎて関所が閉じている")

        with self._failing_once(w):
            self._flush_once(w)

        self.assertTrue(w._pending_output.get("dev"),
                        "前提: 描き残しがある")
        self.assertEqual((gate.is_set(), w._output_timer.isActive()),
                         (True, True),
                         "関所の開け直しと排出のタイマーが、例外で飛ばされた")

    def test_a_failed_render_still_finishes_a_stopped_log(self):
        """停止した記録でも、描画が失敗した区間が書かれてから閉じられること。"""
        w = self._widget()
        path = self._start_recording(w)
        count = w.OUTPUT_SLICE // LINE_LEN
        w.queue_output("dev", self._lines("AAAA", count))
        w.queue_output("dev", self._lines("BBBB", count))
        w.stop_log_recording("dev")
        self.assertTrue(w._closing_logs.get("dev"),
                        "前提: 停止より前の受信が残っている")

        with self._failing_once(w):
            self._flush_once(w)
        while w._pending_output:
            w._flush_pending_output()

        body = open(path, encoding="utf-8").read()
        self.assertEqual((body.count("AAAA"), body.count("BBBB")),
                         (count, count),
                         "停止した記録に、描画が失敗した区間が入っていない")


if __name__ == "__main__":
    unittest.main()
