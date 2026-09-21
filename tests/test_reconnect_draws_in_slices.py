"""再接続のときに溜まり分を一括で描いて固まらないことを検証する。

何が起きていたか（検証役の実測: scratchpad\\cx5j-verify-termui\\
t06c_reconnect_freeze.py、offscreen）: SSH の 1 回の受信ぶん（約 4KB）を
4000 回積んで 16,640,000 文字を溜めた状態で、再接続と同じ
create_terminal_tab('dev') を呼ぶと

    reconnect (_draw_pending_now) froze for 11.90 s (16640000 chars)

_draw_pending_now は pending.take() を上限なしで呼び、溜まった全量を 1 回の
append_output で描いていた。その間イベントループへ戻らないので、画面も
停止操作も固まる。しかも描いた大半は直後に 20000 行の上限で捨てられる。

利用者の決定（2026-09-20）: 再接続時に溜まりを一括で描く件も直す
（描き待ちと同じく分けて描く）。

どう直したか: _flush_pending_output と同じく OUTPUT_SLICE 文字ずつ描き、
片ごとにイベントループへ譲る（利用者の操作は配らないので、譲っている間に
再接続やタブ閉じで入り直さない）。描く順番も描く量も変わらない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class ReconnectDrawsInSlicesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _discard(w):
        w._output_timer.stop()
        w._pending_output.clear()
        w.close()

    def _widget_with_backlog(self, lines=24000):
        """描き待ちを溜めた端末を作り、最後の行の目印を返す。

        受信は SSH の 1 回ぶん（4096 文字）ずつ渡す。
        """
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(self._discard, w)
        w.create_terminal_tab("dev")
        payload = "".join("line %06d %s\r\n" % (i, "x" * 60)
                          for i in range(lines))
        for i in range(0, len(payload), 4096):
            w.queue_output("dev", payload[i:i + 4096])
        # 溜まり分を描くタイマーは止めておく（再接続の描画だけを見る）
        w._output_timer.stop()
        self.assertGreater(len(w._pending_output.get("dev", ())), 1 << 20,
                           "前提: 1MiB 以上の描き待ちがある")
        return w, "line %06d" % (lines - 1)

    def test_the_reconnect_draw_returns_to_the_event_loop(self):
        """溜まり分を描いている間、イベントループが回り続けること。"""
        from PyQt6.QtCore import QTimer
        w, last_line = self._widget_with_backlog()

        ticks = []
        ticker = QTimer()
        ticker.setInterval(0)
        ticker.timeout.connect(lambda: ticks.append(1))
        ticker.start()
        try:
            # 再接続はこの呼び方（MainWindow._on_connect_requested と同じ）
            w.create_terminal_tab("dev")
        finally:
            ticker.stop()

        self.assertGreater(
            len(ticks), 10,
            "溜まり分を描き切るまでイベントループへ戻らなかった（%d 回）"
            % len(ticks))

    def test_the_reconnect_draw_leaves_nothing_behind(self):
        """分けて描いても、溜まり分を全部描き終えてから戻ること。"""
        w, last_line = self._widget_with_backlog()

        w.create_terminal_tab("dev")

        self.assertFalse(w._pending_output.get("dev"),
                         "描き残したまま画面を付け直した")
        text = w._terminals["dev"].toPlainText()
        self.assertIn(last_line, text, "最後に受信した行が画面に無い")


if __name__ == "__main__":
    unittest.main()
