"""右クリックの貼り付けが、DEL（0x7f）や C1 制御文字を含むときも確かめることを検証する。

改行が無い 1 行はそのまま送り、制御文字を含むときだけ確かめる。ところが
制御文字の判定は ord(ch) < 0x20 だけで、DEL（0x7f）が漏れていた。この端末は
Backspace を 0x7f として送っているので、DEL 入りのクリップボードを右クリック
で貼ると、確認なしに機器の入力中の文字が消える。

実測（検証役）: クリップボードに '\\x7fshutdown' / '\\x7f\\x7f\\x7f\\x7fno shut'
を入れて端末を右クリックすると、確認ダイアログを出さずにそのまま送った。
C1 の '\\x9bshutdown' も確認なしで送った（UTF-8 の 0xC2 0x9B として届く）。

判定へ 0x7f と C1（0x80-0x9f）を加えた。改行が無いときは「改行ごとに実行され
ます」ではなく、制御文字を含むことを伝える文言にした。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class PasteConfirmsDelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        terminal.set_input_enabled(True)
        self.sent = []
        terminal.key_pressed.connect(self.sent.append)
        return terminal

    def _right_click(self, terminal, clipboard, answer=None):
        """クリップボードに clipboard を入れて右クリックする（確認は answer で閉じる）。"""
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QContextMenuEvent
        from PyQt6.QtWidgets import QApplication, QDialog
        QApplication.clipboard().setText(clipboard)
        self.dialogs = []
        if answer is None:
            answer = QDialog.DialogCode.Rejected

        def fake_exec(dialog, *args, **kwargs):
            self.dialogs.append(dialog)
            return answer

        with mock.patch("PyQt6.QtWidgets.QDialog.exec", fake_exec):
            terminal.contextMenuEvent(QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, QPoint(5, 5)))
        self.app.processEvents()

    def test_a_delete_character_is_confirmed(self):
        """DEL 入りは確かめ、キャンセルなら何も送らないこと。"""
        for clipboard in ("\x7fshutdown", "\x7f\x7f\x7f\x7fno shut"):
            with self.subTest(clipboard=clipboard):
                terminal = self._terminal()
                self._right_click(terminal, clipboard)
                self.assertEqual(len(self.dialogs), 1,
                                 "DEL 入りを確かめずに送った: %r" % self.sent)
                self.assertEqual(self.sent, [], "取り消したのに送った")

    def test_a_c1_control_character_is_confirmed(self):
        """C1 制御文字（0x80-0x9f）入りも確かめること。"""
        terminal = self._terminal()
        self._right_click(terminal, "\x9bshutdown")
        self.assertEqual(len(self.dialogs), 1,
                         "C1 制御文字入りを確かめずに送った: %r" % self.sent)
        self.assertEqual(self.sent, [])

    def test_confirming_sends_the_delete_character_as_is(self):
        """確かめて送れば、DEL もそのまま送ること。"""
        from PyQt6.QtWidgets import QDialog
        terminal = self._terminal()
        self._right_click(terminal, "\x7fshutdown",
                          answer=QDialog.DialogCode.Accepted)
        self.assertEqual("".join(self.sent), "\x7fshutdown")

    def test_without_a_line_break_the_dialog_speaks_of_control_characters(self):
        """改行の無い貼り付けで「改行ごとに実行」と書かず、制御文字のことを伝えること。"""
        from PyQt6.QtWidgets import QLabel
        terminal = self._terminal()
        self._right_click(terminal, "\x7fshutdown")
        texts = " ".join(label.text()
                         for label in self.dialogs[0].findChildren(QLabel))
        self.assertIn("制御文字", texts, "制御文字を含むことを伝えていない")
        self.assertNotIn("改行ごと", texts, "改行が無いのに改行ごとに実行と書いた")

    def test_a_multi_line_paste_keeps_its_message(self):
        """改行を含む貼り付けは、これまでどおり行ごとに実行されると伝えること。"""
        from PyQt6.QtWidgets import QLabel
        terminal = self._terminal()
        self._right_click(terminal, "conf t\nhostname R1\n")
        texts = " ".join(label.text()
                         for label in self.dialogs[0].findChildren(QLabel))
        self.assertIn("改行ごとにコマンドとして実行されます", texts)

    def test_plain_non_ascii_text_is_still_sent_without_asking(self):
        """日本語などの普通の文字だけなら、これまでどおり確かめずに送ること。"""
        terminal = self._terminal()
        self._right_click(terminal, "description 本社-東京 ¥100")
        self.assertEqual(self.dialogs, [])
        self.assertEqual("".join(self.sent), "description 本社-東京 ¥100")


if __name__ == "__main__":
    unittest.main()
