"""折り返しを戻したコピーで、書式付きの形式が古いまま残らないことを検証する。

何が起きていたか（実測、基準 aa38a2b）:
createMimeDataFromSelection は text/plain だけを折り返しを戻した文字列へ
差し替えていた。Qt が一緒に入れる text/html などはそのままなので、
画面を 5 行 20 桁にして '0123456789ABCDEFGHIJklmnop' を選んでコピーすると、
クリップボードの formats は
['text/html', 'text/markdown', 'application/vnd.oasis.opendocument.text',
 'text/plain'] で、text/plain は繋がっているのに text/html は
'0123456789ABCDEFGHIJ' と 'klmnop' の 2 段落に割れたままだった。
Word・Outlook・Excel は貼り付けで text/html を先に取るので、報告書や
チケットへ貼ると公開鍵や長い設定行が割れた形で入る。

どう直したか:
折り返しを戻して中身が変わったときだけ、text/plain 以外の形式を
removeFormat() で落とす。その場合は色が失われるが、行は壊れない。
折り返しが無くて中身が変わらないときは何も落とさないので、これまでどおり
色付きで貼れる。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

LONG = "0123456789ABCDEFGHIJklmnop"


class CopyDropsWrappedHtmlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtWidgets import QApplication, QMessageBox
        QApplication.clipboard().setText("BEFORE")
        for name in ("warning", "information", "critical"):
            patcher = mock.patch.object(
                QMessageBox, name,
                return_value=QMessageBox.StandardButton.Ok)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _terminal(self, rows=5, cols=20):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.resize(900, 500)
        w.show()
        terminal = w.create_terminal_tab("dev")
        self.app.processEvents()
        terminal._screen.set_size(rows, cols)
        return w, terminal

    def _copy_all(self, terminal):
        from PyQt6.QtGui import QTextCursor
        from PyQt6.QtWidgets import QApplication
        cursor = terminal.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        terminal.setTextCursor(cursor)
        terminal.copy()
        return QApplication.clipboard().mimeData()

    def test_html_is_dropped_when_a_wrapped_line_was_joined(self):
        """折り返しを戻したときは、割れたままの text/html を渡さないこと。"""
        w, terminal = self._terminal()
        w.append_output("dev", LONG + "\r\n")
        self.app.processEvents()

        data = self._copy_all(terminal)

        self.assertIn(LONG, data.text(), "前提: 素のテキストは繋がっている")
        self.assertEqual([fmt for fmt in data.formats()
                          if fmt != "text/plain"], [],
                         "折り返しで割れたままの書式付き形式が残っている")

    def test_colours_are_kept_when_nothing_was_joined(self):
        """折り返しが無ければ、これまでどおり書式付きで貼れること。"""
        w, terminal = self._terminal()
        w.append_output("dev", "AAA\r\nBBB\r\n")
        self.app.processEvents()

        data = self._copy_all(terminal)

        self.assertIn("AAA\nBBB", data.text(), "前提: 本物の改行は残る")
        self.assertIn("text/html", data.formats(),
                      "繋いでいないのに書式付き形式まで落としている")


if __name__ == "__main__":
    unittest.main()
