"""現在画面に残っている自動折り返しが、コピーと全ログ保存で切れないことを検証する。

何が起きていたか（実測、基準 f4cad23）:
画面領域は「必ず行数ぶんの高さで描く」ので、_write_screen は画面の各行を
'\\n' で繋いで文書へ書いていた。履歴へ押し出された行は折り返し（wrapped）を
見て 1 行に繋がるのに、まだ画面に残っている行だけは文書の上で 2 ブロックに
割れていて、コピーも全ログ保存もその文書から取り出していた。
画面を 5 行 20 桁にして '0123456789ABCDEFGHIJklmnop' を出した実測では、
クリップボードも保存ファイルも '0123456789ABCDEFGHIJ\\nklmnop' になり、
同じ行が履歴へ押し出された後は 1 行として残る（経路による食い違い）。
900x500 の既定ウィンドウ（68 桁）で 257 文字の公開鍵 1 行をコピーすると
4 行に割れ、貼り付け先で鍵が壊れた。記録（ログ記録）は受信事象から書くので
影響は無く、壊れるのは文書から取り出す 2 経路だけだった。

どう直したか:
文書そのものは画面の高さを保つ必要があるので、取り出し側で戻す。
InteractiveTerminal に unwrapped_text() を足し、画面領域（_region 以降）で
screen.wrapped が立っている行境界の改行だけを落とす。
createMimeDataFromSelection() がこれを使うので、copy()・マウス解放での
コピー・Ctrl+C が一度に直る。save_current_log も toPlainText() ではなく
同じ変換を通した文字列を保存する。履歴側は既に繋がっているので触らない。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

LONG = "0123456789ABCDEFGHIJklmnop"
KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC" + "x" * 200 + " admin@example.com"


class TerminalCopyKeepsWrappedLinesTest(unittest.TestCase):
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

    def _select_all(self, terminal):
        from PyQt6.QtGui import QTextCursor
        cursor = terminal.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        terminal.setTextCursor(cursor)

    def _copy_all(self, terminal):
        from PyQt6.QtWidgets import QApplication
        self._select_all(terminal)
        terminal.copy()
        return QApplication.clipboard().text()

    def _save_all(self, w):
        from PyQt6.QtWidgets import QFileDialog
        path = os.path.join(tempfile.mkdtemp(prefix="termui03-"), "all.log")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(path, "")):
            w.save_current_log()
        return open(path, encoding="utf-8").read()

    def test_copying_a_wrapped_line_on_screen_keeps_it_whole(self):
        """画面で折り返した 1 行をコピーしても、改行が混ざらないこと。"""
        w, terminal = self._terminal()
        w.append_output("dev", LONG + "\r\n")
        self.app.processEvents()
        self.assertIn("0123456789ABCDEFGHIJ\nklmnop", terminal.toPlainText(),
                      "前提: 文書の上では画面の行として割れている")

        clip = self._copy_all(terminal)

        self.assertIn(LONG, clip, "折り返しの位置で改行が入っている")

    def test_saving_all_logs_keeps_a_wrapped_line_whole(self):
        """全ログ保存でも、画面で折り返した 1 行がそのまま残ること。"""
        w, terminal = self._terminal()
        w.append_output("dev", LONG + "\r\n")
        self.app.processEvents()

        saved = self._save_all(w)

        self.assertIn(LONG, saved, "保存したログで行が折り返しの位置で切れた")

    def test_copying_a_long_key_on_screen_keeps_one_line(self):
        """画面に出たままの公開鍵をコピーしても、1 行のままであること。"""
        w, terminal = self._terminal(rows=30, cols=68)
        w.append_output("dev", KEY + "\r\n")
        self.app.processEvents()

        clip = self._copy_all(terminal)

        self.assertIn(KEY, clip, "公開鍵が折り返しの位置で割れている")

    def test_real_line_breaks_are_kept(self):
        """機器が送った本物の改行は、繋がずそのまま残ること。"""
        w, terminal = self._terminal()
        w.append_output("dev", "AAA\r\nBBB\r\n")
        self.app.processEvents()

        clip = self._copy_all(terminal)

        self.assertIn("AAA\nBBB", clip, "本物の改行まで落ちている")

    def test_a_partial_selection_inside_a_wrapped_line_is_joined(self):
        """折り返し行の途中から選んでも、折り返しの改行だけが落ちること。"""
        from PyQt6.QtGui import QTextCursor
        w, terminal = self._terminal()
        w.append_output("dev", LONG + "\r\n")
        self.app.processEvents()
        cursor = terminal.textCursor()
        cursor.setPosition(5)
        cursor.setPosition(len(LONG) + 1, QTextCursor.MoveMode.KeepAnchor)
        terminal.setTextCursor(cursor)

        terminal.copy()

        from PyQt6.QtWidgets import QApplication
        self.assertEqual(QApplication.clipboard().text(), LONG[5:],
                         "途中から選んだときの繋ぎ方が違う")

    def test_a_line_pushed_into_the_history_is_unchanged(self):
        """履歴へ押し出された行は、これまでどおり 1 行のまま取り出せること。"""
        w, terminal = self._terminal()
        w.append_output("dev", LONG + "\r\n" + "\r\n" * 10)
        self.app.processEvents()
        self.assertIn(LONG, terminal.toPlainText(),
                      "前提: 履歴側は文書の上でも 1 行になっている")

        self.assertIn(LONG, self._copy_all(terminal))
        self.assertIn(LONG, self._save_all(w))


if __name__ == "__main__":
    unittest.main()
