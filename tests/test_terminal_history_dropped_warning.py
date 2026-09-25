r"""履歴の差分を捨てたことが「全ログ保存」の欠落警告に届くのを検証する。

tests/test_terminal_history_delta_cap.py の
test_the_save_all_log_warning_is_raised は、名前に反して描画側の配線を
守れていなかった。src/ui/terminal_widget.py の

    if screen.take_history_dropped():
        terminal._log_truncated = True

を丸ごと削っても、あの 2 ファイル 15 件はすべて green のまま通る
(7 周目の検査役が実測)。MAX_NEW_HISTORY == MAX_DOCUMENT_BLOCKS ==
20000 なので、差分で捨てが起きた = 論理行が 20,001 本以上渡った =
文書は必ず上限超過で切り詰められる、という関係が成り立ち、
_trim_document が同じ _log_truncated を立ててしまうため。

ここでは Screen.MAX_NEW_HISTORY だけを小さくして、文書は上限に
まったく届かない (= _trim_document が働かない) 大きさに保ったまま
差分の捨てを起こす。そうすると _log_truncated を立てられるのは
take_history_dropped() の経路だけになり、配線を消せば落ちる。
上限を下げたときの保険、という 5 行の役目もこれで実物になる。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)


class HistoryDroppedWarningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def flood(self, cap, rows=8, reps=40):
        """差分の上限を cap にして、SU の繰り返しを 1 回で描かせる。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        terminal._screen = Screen(rows, 20)
        with mock.patch.object(Screen, "MAX_NEW_HISTORY", cap):
            w.queue_output("dev", "marker\r\n")
            w._flush_pending_output()
            w.queue_output("dev", (ESC + "[%dS" % rows) * reps)
            w._flush_pending_output()
        return w, terminal

    def test_the_warning_is_raised_without_the_document_being_trimmed(self):
        """文書を切り詰めていなくても、差分の捨てだけで警告が立つこと。"""
        w, terminal = self.flood(cap=20)
        self.assertLess(terminal.document().blockCount(),
                        w.MAX_DOCUMENT_BLOCKS,
                        "文書が切り詰められていて、警告の出所が分からない")
        self.assertTrue(getattr(terminal, "_log_truncated", False),
                        "差分を捨てたのに欠落警告が立っていない")

    def test_no_warning_when_nothing_was_dropped(self):
        """同じ入力でも、上限に届かなければ警告が立たないこと。"""
        w, terminal = self.flood(cap=Screen.MAX_NEW_HISTORY)
        self.assertLess(terminal.document().blockCount(),
                        w.MAX_DOCUMENT_BLOCKS)
        self.assertFalse(getattr(terminal, "_log_truncated", False),
                         "何も捨てていないのに欠落警告が立っている")


if __name__ == "__main__":
    unittest.main()
