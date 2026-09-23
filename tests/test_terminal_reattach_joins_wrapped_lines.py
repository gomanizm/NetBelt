"""再接続で履歴になった画面の折り返しが、繋がったまま残ることを検証する。

何が起きていたか（実測、基準 aa38a2b）:
コピーと全ログ保存は unwrapped_text() で画面領域（_region 以降）の自動
折り返しを 1 行へ戻すが、戻せるのは「いまの画面」だけだった。再接続
（create_terminal_tab の同名分岐 → _attach_screen）は前の画面を文書へ
据え置いたまま _region を文書の末尾へ置き直すので、その行はもう
_wrapped_blocks() の範囲外になる。画面を 5 行 20 桁にして
'0123456789ABCDEFGHIJklmnop' を出した実測では、再接続の前は
'0123456789ABCDEFGHIJklmnop' で取り出せたのに、再接続の後は
'0123456789ABCDEFGHIJ\\nklmnop' に戻り、全ログ保存も同じだった。
NetBelt は「Enterキーを押すと再接続します」が普通の操作なので、切断の
直前に画面へ出ていた公開鍵や長い設定行が、割れたまま永久に残っていた。

どう直したか:
画面を作り直す前に、出ていく画面の折り返し行を文書の上で繋いでおく。
_wrapped_blocks() の各ブロックの区切り文字を、ブロック番号の大きい方から
消す（押し出された履歴と同じ形）。以後はただの履歴として正しく取り出せる。
新しいタブの経路では _screen がまだ無く、_wrapped_blocks() が空集合を
返すので何も起きない。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

LONG = "0123456789ABCDEFGHIJklmnop"


class ReattachJoinsWrappedLinesTest(unittest.TestCase):
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

    def _reconnect(self, w, rows=5, cols=20):
        """同じ機器名で create_terminal_tab を呼び直す（再接続の経路）。"""
        terminal = w.create_terminal_tab("dev")
        self.app.processEvents()
        terminal._screen.set_size(rows, cols)
        return terminal

    def _copy_all(self, terminal):
        from PyQt6.QtGui import QTextCursor
        from PyQt6.QtWidgets import QApplication
        cursor = terminal.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        terminal.setTextCursor(cursor)
        terminal.copy()
        return QApplication.clipboard().text()

    def _save_all(self, w):
        from PyQt6.QtWidgets import QFileDialog
        path = os.path.join(tempfile.mkdtemp(prefix="termui03r-"), "all.log")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(path, "")):
            w.save_current_log()
        return open(path, encoding="utf-8").read()

    def test_a_wrapped_line_stays_whole_after_a_reconnect(self):
        """再接続で履歴になった折り返し行も、1 行のままコピーできること。"""
        w, terminal = self._terminal()
        w.append_output("dev", LONG + "\r\n")
        self.app.processEvents()
        self.assertIn(LONG, self._copy_all(terminal),
                      "前提: 再接続の前は 1 行で取り出せる")

        self._reconnect(w)

        self.assertIn(LONG, self._copy_all(terminal),
                      "再接続のあと、折り返しの位置で行が割れたまま残っている")

    def test_saving_all_logs_after_a_reconnect_keeps_the_line(self):
        """再接続の後の全ログ保存でも、折り返し行が 1 行で残ること。"""
        w, terminal = self._terminal()
        w.append_output("dev", LONG + "\r\n")
        self.app.processEvents()

        self._reconnect(w)

        self.assertIn(LONG, self._save_all(w),
                      "保存したログで、再接続の前の行が割れている")

    def test_real_line_breaks_survive_a_reconnect(self):
        """機器が送った本物の改行は、再接続で繋がれないこと。"""
        w, terminal = self._terminal()
        w.append_output("dev", "AAA\r\nBBB\r\n")
        self.app.processEvents()

        self._reconnect(w)

        self.assertIn("AAA\nBBB", self._copy_all(terminal),
                      "本物の改行まで繋がれている")

    def test_the_new_screen_writes_below_the_joined_history(self):
        """繋いだ後も、再接続した画面の出力がその下へ普通に出ること。"""
        w, terminal = self._terminal()
        w.append_output("dev", LONG + "\r\n")
        self.app.processEvents()

        self._reconnect(w)
        w.append_output("dev", "AFTER\r\n")
        self.app.processEvents()

        clip = self._copy_all(terminal)
        self.assertIn(LONG, clip, "繋いだ履歴が壊れている")
        self.assertLess(clip.index(LONG), clip.index("AFTER"),
                        "再接続の後の出力が、履歴より前に出ている")


if __name__ == "__main__":
    unittest.main()
