r"""行末の全角の前半桁から EL 1 で行が空になったとき、折り返しの印を外すことを検証する。

EL (_erase_line) は、消す範囲の端が全角の途中なら、その全角を丸ごと消す
(_split_wide)。EL 1 で行が丸ごと空になったら次の行への続きは無いので
折り返しの印を外すが、その判定は _split_wide で広がる前の範囲の右端で
行っていた。全角が行末の 2 セルにかかって折り返した行で、その前半桁から
EL 1 を受けると、後半桁も消えて行は全部空白になるのに、印だけが残った。

実測: Screen(4, 4) に 'AB界X' ESC[1;3H ESC[1K を流すと、第 1 行は全セル
空白なのに wrapped[0] は True のまま。続けて ESC[4;1H '\r\n\r\n' で押し
出すと、take_new_history は [('    ', True), ('X   ', False)] で、受信の
経路 (TerminalWidget) では文書の先頭が '    X' の 1 行に繋がった (空行が
消え、X の前に空白が混ざる)。最終桁 (半角) や全角の後半桁からの EL 1 では
印は正しく外れていた。

直し方: _split_wide で消える前に、消す範囲の右端が全角の前半で終わるか
(次のセルが継続セルか) を見て、実際に消える右端を求め、それが行末まで
届いたら印を外す。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
WIDE = chr(0x754C)      # 界


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def history_rows(screen):
    return [("".join(cell[0] for cell in line).rstrip(), wrapped)
            for line, wrapped in screen.take_new_history()]


class EraseToCursorOnWideHeadTest(unittest.TestCase):
    def test_the_emptied_row_loses_its_wrap_mark(self):
        """前半桁からの EL 1 で行が空になったら、折り返しの印が外れること。"""
        s = feed(Screen(4, 4), "AB" + WIDE + "X" + ESC + "[1;3H" + ESC + "[1K")
        self.assertEqual(s.text(), ["", "X", "", ""])
        self.assertFalse(s.wrapped[0], "空になった行に折り返しの印が残っている")

    def test_the_emptied_row_is_not_joined_in_history(self):
        """押し出した履歴で、空になった行が次の行と繋がらないこと。"""
        s = feed(Screen(4, 4), "AB" + WIDE + "X" + ESC + "[1;3H" + ESC + "[1K"
                 + ESC + "[4;1H\r\n\r\n")
        self.assertEqual(history_rows(s), [("", False), ("X", False)])

    def test_content_right_of_the_wide_char_keeps_the_mark(self):
        """全角の右にまだ中身が残るなら、折り返しの印は残すこと。"""
        s = feed(Screen(4, 6), "AB" + WIDE + "CDX" + ESC + "[1;3H"
                 + ESC + "[1K")
        self.assertEqual(s.text(), ["    CD", "X", "", ""])
        self.assertTrue(s.wrapped[0])


class EraseToCursorOnWideHeadThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_rows_apart(self):
        """受信の経路でも、文書で空行と次の行が繋がらないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        rows, cols = terminal._screen.rows, terminal._screen.cols
        text = ("a" * (cols - 2) + WIDE + "X"
                + ESC + "[1;%dH" % (cols - 1) + ESC + "[1K"
                + ESC + "[%d;1H" % rows + "\r\n" * rows)
        w.queue_output("dev", text)
        w._flush_pending_output()
        lines = terminal.toPlainText().split("\n")
        self.assertEqual([line.rstrip() for line in lines[:2]], ["", "X"],
                         "空になった行と次の行が 1 行に繋がっている")


if __name__ == "__main__":
    unittest.main()
