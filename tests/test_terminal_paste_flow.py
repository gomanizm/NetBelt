"""コピーしてすぐ貼り付ける、という実際の操作の流れを通しで検証する。

報告された症状:
  1. show ip route と手で打つ
  2. その文字列を範囲選択してコピーする
  3. 貼り付けると、1回目に打った show ip route の位置に上書きで表示される
  4. もう一度貼り付けると正しい位置に出るが、機器には2回送られており
     コマンドラインが show ip routeshow ip route になる

表示だけの問題に見えて、実際には二重送信を誘発する。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

CMD = "show ip route"


class PasteAfterCopyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _session(self):
        """手で CMD を打ち、機器がエコーを返し終わった状態を作る。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        terminal = w._terminals["dev"]
        terminal.set_input_enabled(True)

        w.append_output("dev", "Router#")
        for ch in CMD:
            w.append_output("dev", ch)
        return w, terminal

    def _copy(self, terminal, needle):
        """needle を範囲選択してクリップボードへ入れる（コピー操作）。"""
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QTextCursor
        start = terminal.toPlainText().index(needle)
        cursor = terminal.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(start + len(needle),
                           QTextCursor.MoveMode.KeepAnchor)
        terminal.setTextCursor(cursor)
        QApplication.clipboard().setText(needle)

    def _paste_and_echo(self, w, terminal):
        """貼り付けて、機器が1文字ずつ返すエコーを流し込む。"""
        sent = []
        terminal.key_pressed.connect(sent.append)
        terminal.custom_paste()
        terminal.key_pressed.disconnect()
        for ch in sent:
            w.append_output("dev", ch)
        return sent

    def test_the_first_paste_does_not_overwrite_the_copied_text(self):
        """1回だけ貼り付けたとき、元のコマンドが消えないこと。"""
        w, terminal = self._session()
        self._copy(terminal, CMD)
        w.append_output("dev", "\r\nCodes: L - local\r\nRouter#")

        self._paste_and_echo(w, terminal)

        text = terminal.toPlainText()
        self.assertEqual(text.count(CMD), 2,
                         "元のコマンドが消えた、または貼り付け分が出ていない: %r"
                         % text)
        self.assertTrue(text.rstrip().endswith("Router#" + CMD),
                        "貼り付け分が末尾のプロンプトに出ていない: %r" % text)

    def test_one_paste_sends_the_command_once(self):
        """貼り付け1回で機器へ送られるのも1回だけであること。"""
        w, terminal = self._session()
        self._copy(terminal, CMD)
        w.append_output("dev", "\r\nCodes: L - local\r\nRouter#")

        sent = self._paste_and_echo(w, terminal)

        self.assertEqual("".join(sent), CMD)

    def test_a_second_paste_is_visible_as_a_second_command(self):
        """2回貼り付ければ2回分が並ぶこと（利用者が意図した場合）。

        以前は1回目が見えないため利用者がもう一度貼り付けてしまい、
        結果として show ip routeshow ip route が機器へ渡っていた。
        2回押したときにそう見えること自体は正しい挙動なので、
        それを固定しておく。
        """
        w, terminal = self._session()
        self._copy(terminal, CMD)
        w.append_output("dev", "\r\nCodes: L - local\r\nRouter#")

        self._paste_and_echo(w, terminal)
        self._paste_and_echo(w, terminal)

        text = terminal.toPlainText()
        self.assertTrue(text.rstrip().endswith("Router#" + CMD + CMD),
                        "2回分が末尾に並んでいない: %r" % text)


if __name__ == "__main__":
    unittest.main()
