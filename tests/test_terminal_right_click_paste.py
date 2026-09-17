"""端末の右クリックが Tera Term と同じく貼り付けになることを検証する。

右クリックはメニュー（コピー／貼り付け／すべて選択／キープアライブ／マクロ／
ログ）を開いていた。範囲選択した時点でコピーされるようにしたので、右クリックは
貼り付けにする（利用者判断 2026-09-17）。

  - 改行を含まない 1 行は、そのまま送る（Enter を押すまで実行されない）
  - 改行を含むと、機器は行ごとにコマンドとして実行する。誤って貼っても
    取り消せないので、送る内容を見せて確かめてから送る
  - 送れない状態（未接続・再接続待ち）では何もしない
  - キーボードのメニューキーでは貼り付けない（押し間違いで送らない）
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class TerminalRightClickPasteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self, connected=True, reconnecting=False):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        terminal.set_input_enabled(connected)
        if reconnecting:
            terminal.set_reconnect_mode(True)
        self.sent = []
        terminal.key_pressed.connect(self.sent.append)
        return w, terminal

    def _right_click(self, terminal, clipboard, answer=None, keyboard=False):
        """クリップボードに clipboard を入れて右クリックする。

        確認ダイアログは answer（Accepted / Rejected）で閉じたことにし、
        出たダイアログを self.dialogs に、開いたメニューを self.menus に残す。
        """
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QContextMenuEvent
        from PyQt6.QtWidgets import QApplication, QDialog
        QApplication.clipboard().setText(clipboard)
        self.dialogs, self.menus = [], []
        if answer is None:
            answer = QDialog.DialogCode.Rejected

        def fake_dialog_exec(dialog, *args, **kwargs):
            self.dialogs.append(dialog)
            return answer

        def fake_menu_exec(menu, *args, **kwargs):
            self.menus.append([a.text() for a in menu.actions()])
            return None

        reason = (QContextMenuEvent.Reason.Keyboard if keyboard
                  else QContextMenuEvent.Reason.Mouse)
        with mock.patch("PyQt6.QtWidgets.QDialog.exec", fake_dialog_exec), \
                mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_menu_exec):
            terminal.contextMenuEvent(QContextMenuEvent(reason, QPoint(5, 5)))
        self.app.processEvents()

    def test_one_line_is_pasted_without_asking(self):
        """改行の無い 1 行は、確かめずにそのまま送ること（メニューも出さない）。"""
        w, terminal = self._terminal()

        self._right_click(terminal, "show version")

        self.assertEqual("".join(self.sent), "show version",
                         "右クリックで貼り付けられていない")
        self.assertEqual(self.dialogs, [], "1 行なのに確認を出した")
        self.assertEqual(self.menus, [], "右クリックでメニューを開いた")

    def test_lines_with_newlines_are_shown_before_sending(self):
        """改行を含むと、送る内容を見せて確かめること。既定はキャンセル。"""
        from PyQt6.QtWidgets import QPlainTextEdit, QPushButton
        w, terminal = self._terminal()

        self._right_click(terminal, "conf t\r\nhostname R1\r\n")

        self.assertEqual(len(self.dialogs), 1, "複数行なのに確かめずに送った")
        self.assertEqual(self.sent, [], "取り消したのに送った")
        dialog = self.dialogs[0]
        shown = [e.toPlainText() for e in dialog.findChildren(QPlainTextEdit)]
        self.assertEqual(shown, ["conf t\nhostname R1\n"],
                         "送る内容がそのまま見えていない")
        default = [b.text() for b in dialog.findChildren(QPushButton)
                   if b.isDefault()]
        self.assertEqual(default, ["キャンセル"],
                         "Enter で送れてしまう（既定ボタンがキャンセルでない）")

    def test_confirming_sends_every_line(self):
        """確かめて送ると、改行は端末と同じ CR で送ること。"""
        from PyQt6.QtWidgets import QDialog
        w, terminal = self._terminal()

        self._right_click(terminal, "conf t\nhostname R1\n",
                          answer=QDialog.DialogCode.Accepted)

        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual("".join(self.sent), "conf t\rhostname R1\r")

    def test_one_line_ending_in_a_newline_is_confirmed_too(self):
        """1 行でも末尾に改行があれば実行されるので、確かめること。"""
        w, terminal = self._terminal()

        self._right_click(terminal, "reload\n")

        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(self.sent, [])

    def test_nothing_happens_when_the_tab_cannot_send(self):
        """未接続・再接続待ちでは、確認も送信もしないこと。"""
        for label, kwargs in (("未接続", {"connected": False}),
                              ("再接続待ち", {"reconnecting": True})):
            with self.subTest(label):
                w, terminal = self._terminal(**kwargs)
                self._right_click(terminal, "show clock\nreload\n")
                self.assertEqual(self.sent, [])
                self.assertEqual(self.dialogs, [])
                self.assertEqual(self.menus, [])

    def test_the_menu_key_does_not_paste(self):
        """キーボードのメニューキーでは貼り付けないこと。"""
        w, terminal = self._terminal()

        self._right_click(terminal, "show version", keyboard=True)

        self.assertEqual(self.sent, [])
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.menus, [])


if __name__ == "__main__":
    unittest.main()
