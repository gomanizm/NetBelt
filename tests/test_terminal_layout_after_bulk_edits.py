"""描画を 1 回の編集にまとめたあとも、文書の組版と書式が正しいことを検証する。

_render_screen は押し出し行・画面領域・書式の塗り直しを 1 回の編集
（beginEditBlock/endEditBlock）にまとめ、先頭の切り捨てを後で 1 回だけ行う。
独立した検証役が次を確かめた。

- 空の文書（新しいタブ）への最初の 1 片で行数の上限を超えると、編集が
  文書全体を覆い、Qt は組版を少しずつ遅れて進める。その途中で先頭を削ると、
  残りのブロックが組版されないまま残り、記録が見えず（高さ 0）、スクロールの
  範囲も狂ったまま直らなかった。ESC[nS（1 回で画面の行数ぶんを記録へ送る）が
  並ぶと、本番の上限 20000 行でも 16384 文字の 1 片で起きる。
- 次の 3 つは、壊しても既存のテストが 1 本も落ちない部品だった
  （検証役の差分ハーネスでだけ差が出た）。
  * 描画が例外で抜けても編集を閉じる（閉じないと以後の組版が止まる）
  * まとめて書くとき、改行は既定の書式の区間にだけ繋ぐ（色付きの区間に
    繋ぐと、改行の書式＝次の行の書式が色付きになる）
  残る 1 つ（改行の無い出力を MAX_BLOCK_CHARS で区切る位置を、段落区切り
  U+2029 などの後ろから数える）は、変更前の区切り方そのものが段落区切りの
  後ろで細かく分かれる形で、テストで固定する形を決められなかった。機器の
  出力に U+2029 が出ることはまず無いので、検証役の突き合わせでの一致に任せる。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

ESC = chr(27)


class LayoutAfterBulkEditsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, cap=None):
        from ui.terminal_widget import TerminalWidget
        if cap is not None:
            patcher = mock.patch.object(TerminalWidget, "MAX_DOCUMENT_BLOCKS", cap)
            patcher.start()
            self.addCleanup(patcher.stop)
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.resize(900, 520)
        w.show()
        terminal = w.create_terminal_tab("dev")
        self.app.processEvents()
        return w, terminal

    def _settle(self, w, seconds=3.0):
        """溜まり分を描き切り、組版が落ち着くまでイベントループを回す。"""
        doc = w._terminals["dev"].document()
        last, since = None, time.time()
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            size = (doc.size().height(), doc.blockCount())
            busy = bool(w._pending_output) or w._output_timer.isActive()
            if size != last or busy:
                last, since = size, time.time()
            elif time.time() - since > 0.2:
                break
            time.sleep(0.005)

    @staticmethod
    def _unlaid(terminal):
        """文字があるのに組版されていない（高さ 0 の）ブロックの番号。"""
        out = []
        block = terminal.document().begin()
        while block.isValid():
            if block.text() and block.layout().lineCount() == 0:
                out.append(block.blockNumber())
            block = block.next()
        return out

    def test_a_first_slice_over_the_limit_leaves_every_line_laid_out(self):
        """新しいタブの最初の 1 片で上限を超えても、全行が組版され最下部を追えること。"""
        w, terminal = self._widget()
        rows = terminal._screen.rows
        unit = "line %05d\r\n" + ESC + "[%dS" % rows
        parts, i = [], 0
        while sum(map(len, parts)) < 16000:
            parts.append(unit % i)
            i += 1
        w.queue_output("dev", "".join(parts) + "prompt# ")
        self._settle(w)

        doc = terminal.document()
        self.assertEqual(doc.blockCount(), w.MAX_DOCUMENT_BLOCKS,
                         "前提: 最初の 1 片で上限まで切り捨てている")
        unlaid = self._unlaid(terminal)
        self.assertEqual(unlaid[:5], [],
                         "組版されないまま残ったブロックが %d 個ある" % len(unlaid))
        bar = terminal.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum(), "最下部を見ていない")

    def test_layout_goes_on_after_a_failed_render(self):
        """描画が例外で抜けても、以後の出力が組版されて最下部を追えること。"""
        w, terminal = self._widget()
        real = w._paint_row
        state = {"fail": True}

        def failing(*args, **kwargs):
            if state["fail"]:
                raise RuntimeError("paint failed")
            return real(*args, **kwargs)

        w._paint_row = failing
        with mock.patch("sys.excepthook"):
            try:
                w.append_output("dev", "".join("x%04d\r\n" % i for i in range(200)))
            except RuntimeError:
                pass
        state["fail"] = False
        w.queue_output("dev", "".join("y%04d\r\n" % i for i in range(600)))
        self._settle(w)

        self.assertEqual(self._unlaid(terminal)[:5], [], "失敗のあと組版が止まった")
        bar = terminal.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum(), "失敗のあと最下部を追えていない")

    def test_a_coloured_line_end_does_not_colour_the_next_line(self):
        """色付きの行が記録へ送られても、次の行（空行）の書式は既定のままであること。"""
        from PyQt6.QtGui import QTextFormat
        w, terminal = self._widget()
        w.append_output("dev", ESC + "[31mRED LINE\r\n" + ESC + "[0m\r\n"
                        + "".join("plain %03d\r\n" % i for i in range(120)))
        self.app.processEvents()

        doc = terminal.document()
        block = doc.begin()
        while block.isValid() and block.text() != "RED LINE":
            block = block.next()
        self.assertTrue(block.isValid(), "前提: 色付きの行が記録に残っている")
        following = block.next()
        self.assertEqual(following.text(), "", "前提: 次は空行")
        self.assertFalse(
            following.charFormat().hasProperty(QTextFormat.Property.ForegroundBrush),
            "色付きの行の改行が、次の行へ色を持ち込んだ")

if __name__ == "__main__":
    unittest.main()
