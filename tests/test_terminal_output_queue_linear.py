"""受信出力の溜まりを描き切るまでの手間が、溜まった量に比例する（2 乗にならない）ことを検証する。

_flush_pending_output は 1 回に OUTPUT_SLICE（16384）文字ずつ描く。ところが毎回
溜まっているかたまりを全部つなぎ、描く分を除いた残り全体を切り出して戻して
いたので、溜まった N 文字を描き切るまでのコピー量がおよそ N^2 / (2 x 16384)
になっていた。

実測（検証役、描画を文字数を数えるだけの偽物にして溜まりの処理だけを回した）:
1MiB 0.00 秒、4MiB 0.08 秒（18.6us/KiB）、16MiB 1.08 秒（65.8）、32MiB 4.57 秒
（139.5）、64MiB 18.03 秒（275.1）。KiB あたりの手間が溜まり量に比例して増えた。
LAN 越しの SSH で巨大なファイルを cat したときなど、数十 MB 溜まると描画の
手間に 1〜4 割が上乗せされた。

溜まりを「かたまりの列 + 先頭のかたまりの読み始め位置」で持ち、先頭から
描く分だけを取り出すようにした（全体をつないだり残りを切り出したりしない）。
"""
import gc
import os
import sys
import time
import unittest

sys.path.insert(0, "src")

PIECE = "x" * 4095 + "\n"      # 受信スレッドと同じくらいのかたまり


class OutputQueueLinearTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        """描画を、受け取った文字を控えるだけの偽物に替えた端末を返す。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("dev")
        self.drawn = []

        def fake_append(device_name, text):
            w._flushing_device = None
            self.drawn.append(text)

        w.append_output = fake_append
        return w

    def _drain_seconds(self, w, mib):
        """mib MiB を受信経路で溜め、溜まりの処理だけで描き切るまでの秒数。"""
        for _ in range(mib * 1024 * 1024 // len(PIECE)):
            w.queue_output("dev", PIECE)
        w._output_timer.stop()
        del self.drawn[:]
        # 測っている間は GC を止める。全テストを続けて流すと生きている
        # オブジェクトが多く、途中で走った GC の時間が測定を乱した
        # （4MiB の排出は 1ms 足らずなので、1 回で比が 2 倍を超えた）
        gc.disable()
        try:
            started = time.perf_counter()
            while w._pending_output:
                w._flush_pending_output()
            elapsed = time.perf_counter() - started
        finally:
            gc.enable()
        w._output_timer.stop()
        return elapsed

    def test_draining_time_per_kib_does_not_grow_with_the_backlog(self):
        """32MiB を描き切る手間（KiB あたり）が、8MiB のときの 2 倍に届かないこと。

        直す前は 8MiB と 32MiB で 4 倍前後（手間が溜まり量に比例して増える）。
        """
        w = self._widget()
        small = min(self._drain_seconds(w, 8) for _ in range(3)) / (8 * 1024)
        large = min(self._drain_seconds(w, 32) for _ in range(2)) / (32 * 1024)
        self.assertLess(large / small, 2.0,
                        "溜まり量で KiB あたりの手間が増えた: 8MiB %.2fus/KiB、"
                        "32MiB %.2fus/KiB" % (small * 1e6, large * 1e6))

    def test_slices_come_out_whole_and_in_order(self):
        """取り出す片は OUTPUT_SLICE 文字ずつで、順序も中身も変わらないこと。"""
        w = self._widget()
        sent = ["%05d:" % i + "y" * (i % 7000) + "\r\n" for i in range(600)]
        for text in sent:
            w.queue_output("dev", text)
        w._output_timer.stop()
        while w._pending_output:
            w._flush_pending_output()
        w._output_timer.stop()

        self.assertEqual("".join(self.drawn), "".join(sent), "中身か順序が変わった")
        self.assertTrue(all(len(part) == w.OUTPUT_SLICE for part in self.drawn[:-1]),
                        "最後以外の片が OUTPUT_SLICE 文字になっていない")

    def test_a_notice_during_a_backlog_is_drawn_after_it(self):
        """溜まりがある間に直接描こうとした案内は、溜まりの後ろに並ぶこと。"""
        from ui.terminal_widget import TerminalWidget
        w = self._widget()
        w.append_output = lambda *args: TerminalWidget.append_output(w, *args)
        drawn = []
        w._render_screen = lambda terminal: drawn.append(
            "".join(cell[0] for row in terminal._screen.lines for cell in row))
        w.queue_output("dev", "A" * (w.OUTPUT_SLICE + 10))
        w.append_output("dev", "NOTICE")
        w._output_timer.stop()
        self.assertEqual(drawn, [], "前提: 溜まりがある間は描いていない")
        while w._pending_output:
            w._flush_pending_output()
        w._output_timer.stop()

        self.assertEqual(len(drawn), 2, "溜まりの後ろへ並ばずに描いた")
        self.assertIn("NOTICE", drawn[-1], "案内が最後の片に入っていない")


if __name__ == "__main__":
    unittest.main()
