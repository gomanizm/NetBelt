r"""ED 0 で画面が丸ごと空になるとき、消える前の中身を履歴へ送ることを検証する。

画面全体が消えるとき (ED 2、原点からの ED 0、下に中身が残らない ED 1) は、
消す前に見えていた中身を履歴へ送る (clear でセッションの記録を失わない、
という v1.1.1 の方針)。ところが ED 0 は原点 (1 行 1 桁) からの消去しか
その扱いにしておらず、カーソルより前がすべて空白という画面で原点以外から
ESC[J が来ると、画面は丸ごと空になるのに何も記録されなかった。

実測: Screen(3, 10) に '\r\nKEEP' ESC[2;1H ESC[J を流すと、画面は全行空白、
history も take_new_history() も空だった。同じ行の手前が空白の位置
('\r\n    KEEP' から ESC[2;3H ESC[J) でも同じ。受信の経路 (queue_output →
_flush_pending_output) でも文書から KEEP が消え、後の出力のあとも戻らず、
文書の toPlainText() を書き出すログ保存にも残らなかった。起きるのは、
接続直後の 1 画面目で先頭行が空のときや、ホームへ戻らない ESC[2J の後に
最下行へ出力して '\r' ESC[J を受けたときなど。

直し方: ED 1 の「カーソルより下がすべて空白なら記録する」と対称に、ED 0 でも
上の行すべてと、カーソル行の (全角の後半なら前半へ丸めた) 桁より前が
すべて空白なら、消す前に画面を履歴へ送る。消し方は今までの ED 0 のまま。
手前に中身が残るときは、画面が空にならないので今までどおり記録しない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
KANJI = chr(0x6F22)


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def row_text(line):
    return "".join(cell[0] for cell in line).rstrip()


class EraseBelowWithBlankBeforeTest(unittest.TestCase):
    def test_blank_rows_above_record_the_wiped_screen(self):
        """上の行が空白で、行頭から ED 0 したら中身が履歴へ残ること。"""
        s = feed(Screen(3, 10), "\r\nKEEP" + ESC + "[2;1H" + ESC + "[J")
        self.assertEqual(s.text(), ["", "", ""])
        self.assertEqual([row_text(line) for line, _ in s.take_new_history()],
                         ["", "KEEP"],
                         "画面が丸ごと空になったのに記録されていない")
        self.assertEqual([row_text(line) for line in s.history], ["", "KEEP"])

    def test_blank_cells_before_the_cursor_on_its_row(self):
        """カーソル行の手前が空白の位置からでも、記録されること。"""
        s = feed(Screen(3, 10),
                 "\r\n    KEEP" + ESC + "[2;3H" + ESC + "[J")
        self.assertEqual(s.text(), ["", "", ""])
        self.assertEqual([row_text(line) for line in s.history],
                         ["", "    KEEP"])

    def test_cursor_on_the_second_half_of_a_wide_char(self):
        """全角の後半桁からの ED 0 は前半桁から消えるので、そこから数えること。"""
        s = feed(Screen(3, 10),
                 "\r\n" + KANJI + "KEEP" + ESC + "[2;2H" + ESC + "[J")
        self.assertEqual(s.text(), ["", "", ""])
        self.assertEqual([row_text(line) for line in s.history],
                         ["", KANJI + "KEEP"])

    def test_content_above_is_left_alone(self):
        """上の行に中身が残るなら、画面は空にならないので記録しないこと。"""
        s = feed(Screen(3, 10), "A\r\nKEEP" + ESC + "[2;1H" + ESC + "[J")
        self.assertEqual(s.text(), ["A", "", ""])
        self.assertEqual(list(s.history), [])
        self.assertEqual(s.take_new_history(), [])

    def test_content_before_the_cursor_on_its_row_is_left_alone(self):
        """カーソル行の手前に中身が残るなら、記録しないこと。"""
        s = feed(Screen(3, 10), "\r\nAB KEEP" + ESC + "[2;3H" + ESC + "[J")
        self.assertEqual(s.text(), ["", "AB", ""])
        self.assertEqual(list(s.history), [])

    def test_prompt_after_home_less_erase_display(self):
        """ホームへ戻らない ESC[2J のあと最下行へ出た行も、CR と ESC[J で失わないこと。"""
        s = Screen(3, 10)
        feed(s, ESC + "[3;1H" + ESC + "[2J" + "$ show")
        s.take_new_history()
        feed(s, "\r" + ESC + "[J")
        self.assertEqual(s.text(), ["", "", ""])
        self.assertEqual([row_text(line) for line, _ in s.take_new_history()],
                         ["", "", "$ show"])


class EraseBelowThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_the_wiped_line(self):
        """受信の経路でも、消えた行が文書 (ログ保存の元) に残ること。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        w.queue_output("dev", "\r\nKEEP")
        w._flush_pending_output()
        w.queue_output("dev", ESC + "[2;1H" + ESC + "[J")
        w._flush_pending_output()
        w.queue_output("dev", "next")
        w._flush_pending_output()
        text = terminal.toPlainText()
        self.assertIn("KEEP", text, "消えた行が文書から失われている")
        self.assertIn("next", text)


if __name__ == "__main__":
    unittest.main()
