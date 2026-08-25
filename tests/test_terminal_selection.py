"""ユーザーの範囲選択が、機器から届く出力の描画先を狂わせないことを検証する。

描画は terminal.textCursor()（＝ユーザーのカーソル）をそのまま使っていた。
QTextCursor は選択を持ったまま insertText すると選択範囲を置き換えるため、
コピーのために選択したテキストが、機器から返ってきたエコーで潰れていた。
コピー自体は正常なので「ペーストすると表示が壊れる」形で表面化する。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

BS = "\x08"


class TerminalSelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        return w, w._terminals["dev"]

    def _select(self, terminal, needle):
        """terminal 上の needle を範囲選択する（コピー操作の再現）。"""
        from PyQt6.QtGui import QTextCursor
        start = terminal.toPlainText().index(needle)
        cursor = terminal.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(start + len(needle),
                           QTextCursor.MoveMode.KeepAnchor)
        terminal.setTextCursor(cursor)
        return cursor

    def test_device_output_does_not_overwrite_a_selection(self):
        """選択中のテキストが、届いた出力で置き換えられないこと。"""
        w, terminal = self._terminal()
        w.append_output("dev", "Router#show ip int brief\r\n")
        w.append_output("dev", "GigabitEthernet1  unassigned  up  up\r\n")
        w.append_output("dev", "Router#")

        self._select(terminal, "GigabitEthernet1")
        w.append_output("dev", "conf t\r\n")

        self.assertIn("GigabitEthernet1", terminal.toPlainText(),
                      "選択していたテキストが出力で潰された")

    def test_device_output_still_lands_at_the_write_position(self):
        """選択があっても、出力は選択位置ではなく末尾へ書かれること。"""
        w, terminal = self._terminal()
        w.append_output("dev", "Router#show version\r\n")
        w.append_output("dev", "Cisco IOS Software\r\n")
        w.append_output("dev", "Router#")

        self._select(terminal, "Cisco IOS Software")
        w.append_output("dev", "conf t")

        text = terminal.toPlainText()
        self.assertTrue(text.rstrip().endswith("Router#conf t"),
                        "出力が末尾ではない場所へ書かれた: %r" % text)

    def test_a_selection_survives_incoming_output(self):
        """出力が届いても選択が消えないこと（消えるとコピーできない）。

        機器はキープアライブや非同期のログを勝手に送ってくる。選択している
        最中にそれが届くたびに選択が外れると、コピーそのものが成立しない。
        """
        w, terminal = self._terminal()
        w.append_output("dev", "Router#show ip int brief\r\n")
        w.append_output("dev", "GigabitEthernet1  unassigned  up  up\r\n")

        self._select(terminal, "GigabitEthernet1")
        w.append_output("dev", "\r\n*Aug 25 12:00:00: %SYS-5-CONFIG_I: Configured\r\n")

        self.assertTrue(terminal.textCursor().hasSelection(),
                        "出力が届いた拍子に選択が外れた")
        self.assertEqual(
            terminal.textCursor().selectedText(), "GigabitEthernet1")

    def test_control_codes_still_continue_across_calls(self):
        """描画位置は append_output を跨いで保たれること（既存の挙動）。

        機器はカーソルを戻してから、次のパケットで上書き文字を送る。
        呼び出しごとに末尾へ戻してしまうとこの行編集が壊れる。
        """
        w, terminal = self._terminal()
        w.append_output("dev", "shoe running-config")
        w.append_output("dev", BS * 15)
        w.append_output("dev", "X running-config" + BS * 15)
        self.assertIn("shoeX running-config",
                      terminal.toPlainText().replace("\n", ""))


if __name__ == "__main__":
    unittest.main()
