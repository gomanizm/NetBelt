"""再接続の描き切りで描画が失敗しても、関所と排出が続くことを検証する。

何が起きていたか（実測、基準 aa38a2b）:
_flush_pending_output は「描画が失敗しても、受信の関所の開け直しと次の
排出だけは続ける」形へ直したが、再接続のときの描き切り
（_draw_pending_now）には同じ直しが入っていなかった。
_update_output_gate が内側の try/finally の外に置かれたままで、外側にも
タイマーの掛け直しが無い。製品の入口（create_terminal_tab の再接続分岐が
_draw_pending_now を呼ぶ）から実測すると、描き待ち 32,760 文字で
_paint_row を 1 回だけ RuntimeError にしたとき、残り 16,376 文字が
描かれないまま _output_timer.isActive() は False、関所は閉じたまま
（gate.is_set() が False）になった。_output_gates はタブを閉じるまで
機器名で持ち回されるので、次の接続の受信スレッドへ渡るのも同じ閉じた
Event で、その機器は受信できないまま止まる。

どう直したか:
_flush_pending_output と同じ形にした。_update_output_gate を内側の
finally（_flushing_device を戻すところ）へ移し、外側の finally
（_drawing_pending_now を戻すところ）で、描き待ちが残っていれば
_output_timer を掛け直す。例外そのものは握り潰さず、これまでどおり
呼び出し元へ返す。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 1 行 12 文字（"AAAA %05d\r\n"）で、OUTPUT_SLICE 16384 に収まる分を作る
LINE_LEN = len("AAAA %05d\r\n" % 0)


class ReconnectDrawKeepsGateTest(unittest.TestCase):
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
        # 関所が閉じる量まで溜めるのは時間がかかるので、水位を下げておく
        for name, value in (("PENDING_HIGH_WATER", 1000),
                            ("PENDING_LOW_WATER", 500)):
            patcher = mock.patch.object(TerminalWidget, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        w = TerminalWidget()
        self.addCleanup(self._discard, w)
        w.resize(900, 500)
        w.show()
        w.create_terminal_tab("dev")
        self.app.processEvents()
        return w

    @staticmethod
    def _lines(tag, count):
        return "".join("%s %05d\r\n" % (tag, i) for i in range(count))

    def _fill(self, w, tail=None):
        """描き切りが 2 片に分かれる量を溜めて、切断で止まった形にする。

        tail を小さくすると、1 片目が失敗した時点の残りが下の水位を
        下回るので、関所が開き直ったかどうかをその場で見られる。
        """
        count = w.OUTPUT_SLICE // LINE_LEN
        w.queue_output("dev", self._lines("AAAA", count))
        w.queue_output("dev", self._lines("BBBB", count if tail is None
                                          else tail))
        # 切断でイベントループが止まっている間に溜まった形にそろえる
        w._output_timer.stop()
        return count

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

    def _reconnect(self, w):
        """同じ機器名で create_terminal_tab を呼び直す（再接続の経路）。"""
        with self._failing_once(w):
            with self.assertRaises(RuntimeError):
                w.create_terminal_tab("dev")

    def test_a_failed_redraw_on_reconnect_reopens_the_gate(self):
        """再接続の描き切りが失敗しても、関所が開き直り、排出が続くこと。"""
        w = self._widget()
        gate = w.output_gate("dev")
        self._fill(w, tail=8)
        self.assertFalse(gate.is_set(), "前提: 溜まりすぎて関所が閉じている")

        self._reconnect(w)

        self.assertTrue(w._pending_output.get("dev"), "前提: 描き残しがある")
        self.assertEqual((gate.is_set(), w._output_timer.isActive()),
                         (True, True),
                         "関所の開け直しと排出のタイマーが、例外で飛ばされた")

    def test_the_rest_is_drawn_after_a_failed_redraw_on_reconnect(self):
        """再接続の描き切りが失敗しても、残りが描かれて溜まりが空になること。"""
        w = self._widget()
        gate = w.output_gate("dev")
        self._fill(w)

        self._reconnect(w)
        for _ in range(50):
            self.app.processEvents()

        self.assertEqual(len(w._pending_output.get("dev", ())), 0,
                         "描き残しが永久に描かれないまま残っている")
        self.assertIn("BBBB", w._terminals["dev"].toPlainText(),
                      "描き切りの途中で止まり、後ろの受信が画面に出ていない")
        # 次の接続の受信スレッドへ渡るのは同じ Event。閉じたままだと読めない
        self.assertIs(w.output_gate("dev"), gate,
                      "前提: 関所は機器名で持ち回される")
        self.assertTrue(gate.is_set(),
                        "次の接続へ閉じたままの関所が渡り、受信できない")


if __name__ == "__main__":
    unittest.main()
