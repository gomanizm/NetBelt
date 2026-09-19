r"""画面を消す前に記録した最後の行が、次の出力と繋がらないことを検証する。

_record_screen は、画面全体が消える前 (ED 2・原点からの ED 0・RIS など) に、
最後の非空行までを履歴へ送る。その先の空白の行は記録しないのに、最後に
記録する行の折り返しの印はそのまま渡していた。折り返した先の行が空白
だけだと、記録の最後の行は「次の行へ続く」印を持ったまま履歴に入り、
描画側はそれを消去のあとに来た出力と 1 行に繋げた。

実測: Screen(4, 8) に '$ cmd\r\n12345678 \r\n' ESC[H ESC[2J を流すと、
take_new_history() は [('$ cmd', False), ('12345678', True)]。受信の経路
(TerminalWidget、4x8) で続けて 'NEXT\r\n' を出すと、文書は
'$ cmd\n12345678NEXT\n…' になった (消去の前の出力と後の出力が 1 行に
なり、コピーとログにもそのまま残る)。

直し方: 記録を打ち切った最後の行は、折り返しの印を外して履歴へ渡す。
続き (空白の行) は記録しないので、次へ続く先が無い。記録の途中の行は
次の行も記録するので、印はそのまま渡す。画面側の印 (wrapped) には触らない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
OUTPUT = "$ cmd\r\n12345678 \r\n"
CLEAR = ESC + "[H" + ESC + "[2J"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def history_rows(screen):
    return [("".join(cell[0] for cell in line).rstrip(), wrapped)
            for line, wrapped in screen.take_new_history()]


class RecordScreenLastRowMarkTest(unittest.TestCase):
    def test_last_recorded_row_loses_its_wrap_mark(self):
        """記録の最後の行は、次の行へ続く印を持たないこと。"""
        s = feed(Screen(4, 8), OUTPUT + CLEAR)
        self.assertEqual(history_rows(s),
                         [("$ cmd", False), ("12345678", False)],
                         "記録の最後の行に折り返しの印が残っている")

    def test_full_reset_records_the_same_way(self):
        """RIS で記録するときも、最後の行の印を外すこと。"""
        s = feed(Screen(4, 8), OUTPUT + ESC + "c")
        self.assertEqual(history_rows(s),
                         [("$ cmd", False), ("12345678", False)])

    def test_wrapped_rows_inside_the_record_keep_their_mark(self):
        """記録の途中で折り返した行は、次の行と繋がる印を残すこと。"""
        s = feed(Screen(4, 8), "1234567890\r\n" + CLEAR)
        self.assertEqual(history_rows(s),
                         [("12345678", True), ("90", False)])


class RecordScreenLastRowMarkThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_record_apart_from_the_next_output(self):
        """文書で、消去の前の最後の行と消去の後の出力が繋がらないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w._grid_size = lambda t: (4, 8)
        terminal = w.create_terminal_tab("dev")
        w._apply_grid_size("dev")
        self.assertEqual((terminal._screen.rows, terminal._screen.cols), (4, 8))
        w.append_output("dev", OUTPUT + CLEAR + "NEXT\r\n")
        lines = [line.rstrip() for line in terminal.toPlainText().split("\n")]
        self.assertEqual(lines[:3], ["$ cmd", "12345678", "NEXT"],
                         "消去の前の行と後の出力が 1 行に繋がっている")


if __name__ == "__main__":
    unittest.main()
