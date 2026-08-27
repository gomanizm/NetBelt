"""日本語入力（IME）の確定文字が、画面ではなく機器へ行くことを検証する。

InteractiveTerminal は setReadOnly(False) の編集可能な QTextEdit で、
キー入力は keyPressEvent を上書きして横取りし、ドロップ経路は
insertFromMimeData で送信へ振り替えている。

ところが IME の確定文字列は keyPressEvent ではなく inputMethodEvent
経由で入るため、どちらの網にも掛からない。QTextEdit の既定動作がその
まま働き、確定文字が文書へ直接挿入され key_pressed は一度も発火しない。
send_text の説明が防ごうとした「機器が受け取っていない文字が入力済みの
ように見える」状態が、そのまま起きる。

さらに悪いのは、範囲選択がある状態で確定すると QTextEdit が選択範囲を
置換する点で、選択が確定済みの履歴領域に掛かっていると、機器の出力が
その場で書き換わる。_render_screen は画面領域しか描き直さないので
復元されず、文書を読む「全ログ保存」にもそのまま残る。

変換中の未確定文字列（preedit）も文書へ入れてはいけない。端末の画面は
機器の出力を写したものなので、そこへ割り込ませると描画とずれる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

PROMPT = "Router#"


class TerminalImeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _session(self):
        """機器がプロンプトを返し終わった、入力できる状態を作る。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        terminal = w._terminals["dev"]
        terminal.set_input_enabled(True)
        w.append_output("dev", PROMPT)

        sent = []
        terminal.key_pressed.connect(sent.append)
        return w, terminal, sent

    @staticmethod
    def _commit(terminal, text):
        """IME が文字列を確定したときに Qt が送るイベントを起こす。"""
        from PyQt6.QtGui import QInputMethodEvent
        event = QInputMethodEvent()
        event.setCommitString(text)
        terminal.inputMethodEvent(event)

    @staticmethod
    def _preedit(terminal, text):
        """変換中（未確定）の状態を Qt が伝えてくるイベントを起こす。"""
        from PyQt6.QtGui import QInputMethodEvent
        terminal.inputMethodEvent(QInputMethodEvent(text, []))

    def _select(self, terminal, needle):
        """文書内の needle を範囲選択する。"""
        from PyQt6.QtGui import QTextCursor
        start = terminal.toPlainText().index(needle)
        cursor = terminal.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(start + len(needle),
                           QTextCursor.MoveMode.KeepAnchor)
        terminal.setTextCursor(cursor)

    def test_a_committed_string_reaches_the_device(self):
        """確定した文字が機器へ送られること。"""
        _, terminal, sent = self._session()
        self._commit(terminal, "こんにちは")
        self.assertEqual("".join(sent), "こんにちは",
                         "確定した文字が機器へ送られていない")

    def test_a_committed_string_is_not_written_into_the_document(self):
        """確定した文字を画面へ直接書かないこと。

        画面に出てよいのは、機器が返してきたエコーだけ。
        """
        _, terminal, _ = self._session()
        before = terminal.toPlainText()
        self._commit(terminal, "こんにちは")
        self.assertEqual(terminal.toPlainText(), before,
                         "確定した文字が画面へ直接書かれている")

    def test_a_committed_string_does_not_overwrite_the_selected_history(self):
        """履歴を選択したまま確定しても、機器の出力を壊さないこと。

        これが一番おそろしい形。書き換わった行は描き直されないので
        永久に残り、全ログ保存にもそのまま入る。
        """
        _, terminal, _ = self._session()
        self._select(terminal, PROMPT)
        before = terminal.toPlainText()

        self._commit(terminal, "こんにちは")

        self.assertEqual(terminal.toPlainText(), before,
                         "選択していた機器の出力が確定文字で置き換わっている")

    def test_a_preedit_string_is_not_written_into_the_document(self):
        """変換中の未確定文字列を画面へ書かないこと。"""
        _, terminal, _ = self._session()
        before = terminal.toPlainText()
        self._preedit(terminal, "こんにち")
        self.assertEqual(terminal.toPlainText(), before,
                         "変換中の文字が画面へ書かれている")

    def test_a_preedit_string_is_not_sent_to_the_device(self):
        """変換中の未確定文字列を機器へ送らないこと。"""
        _, terminal, sent = self._session()
        self._preedit(terminal, "こんにち")
        self.assertEqual(sent, [], "確定していない文字を送っている")

    def test_nothing_happens_while_input_is_disabled(self):
        """未接続のタブでは、送りも書きもしないこと。

        keyPressEvent の早期 return は inputMethodEvent には効かない。
        """
        _, terminal, sent = self._session()
        terminal.set_input_enabled(False)
        before = terminal.toPlainText()

        self._commit(terminal, "こんにちは")

        self.assertEqual(sent, [], "未接続なのに送っている")
        self.assertEqual(terminal.toPlainText(), before,
                         "未接続なのに画面へ書かれている")

    def test_nothing_happens_while_waiting_to_reconnect(self):
        """再接続待機中も、送りも書きもしないこと。"""
        _, terminal, sent = self._session()
        terminal.set_reconnect_mode(True)
        before = terminal.toPlainText()

        self._commit(terminal, "こんにちは")

        self.assertEqual(sent, [], "再接続待機中なのに送っている")
        self.assertEqual(terminal.toPlainText(), before,
                         "再接続待機中なのに画面へ書かれている")


if __name__ == "__main__":
    unittest.main()
