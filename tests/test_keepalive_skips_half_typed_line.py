"""打ちかけの入力があるうちは、キープアライブの Enter を送らないことを検証する。

何が起きていたか（実測: scratchpad\\cx5b-termui\\t05_06.py）: 端末で 'reload' を
打鍵して Enter を押さずにいると、MacroManager._send_keepalive が走ったときに
機器へ 'reload\\r' が送られ、利用者が Enter を押していないコマンドがそのまま
実行された。キープアライブの CR は打鍵や貼り付けと同じ送信列を通るので、
打ちかけの行の後ろにそのまま付く。

どう直したか（利用者の決定 2026-09-20）: 打ちかけの入力がある間は、その回の
キープアライブを送らずに飛ばす（溜めて後から送ることはしない。次の間隔で
また判断する）。打ちかけとは、利用者の打鍵・貼り付け・IME 確定で機器へ送った
文字のうち、最後の行送り（CR / LF）より後に何か送っている状態。Ctrl+C (0x03) と
Ctrl+U (0x15) は行を捨てる操作なので打ちかけを解く。Backspace のように行が
残っているか判断できない操作は打ちかけのまま扱い、送らない側へ倒す。
マクロとキープアライブ自身の送信は数えない。接続し直したら打ちかけは解く。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class KeepaliveSkipsHalfTypedLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _session(self):
        """入力できる端末と、そこへ登録した MacroManager を作る。"""
        from core.macro_manager import MacroManager
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("dev")
        terminal = w._terminals["dev"]
        terminal.set_input_enabled(True)

        sent = []
        terminal.key_pressed.connect(sent.append)

        manager = MacroManager()
        # main_window._on_connect_requested と同じ登録の仕方
        manager.register_send_callback(
            "dev", terminal._queue_send,
            command_callback=terminal.queue_macro_send,
            cancel_callback=terminal.cancel_macro_sends)
        self.addCleanup(manager.unregister_send_callback, "dev")
        return terminal, manager, sent

    @staticmethod
    def _type(terminal, text):
        """利用者が 1 文字ずつ打鍵したのと同じ経路を通す。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        for ch in text:
            if ch == "\r":
                QTest.keyClick(terminal, Qt.Key.Key_Return)
            else:
                QTest.keyClicks(terminal, ch)

    @staticmethod
    def _control_key(terminal, key, char):
        """Ctrl + 英字の打鍵を起こす。

        QTest.keyClick に ControlModifier を付けても text は 'c' のままで、
        制御文字にならない。実際の Qt がくれるイベントと同じ形へ組み立てる。
        """
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        terminal.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, key,
            Qt.KeyboardModifier.ControlModifier, char))

    @staticmethod
    def _commit(terminal, text):
        """IME が文字列を確定したときに Qt が送るイベントを起こす。"""
        from PyQt6.QtGui import QInputMethodEvent
        event = QInputMethodEvent()
        event.setCommitString(text)
        terminal.inputMethodEvent(event)

    def test_a_half_typed_command_is_not_executed_by_the_keepalive(self):
        """Enter を押していない打鍵の後ろに、キープアライブの CR が付かないこと。"""
        terminal, manager, sent = self._session()
        self._type(terminal, "reload")
        del sent[:]

        manager._send_keepalive("dev")       # タイマーの発火と同じ

        self.assertEqual(sent, [], "打ちかけの 'reload' が実行された: %r" % sent)

    def test_the_keepalive_resumes_once_the_line_is_sent(self):
        """Enter を押した後は、今までどおりキープアライブを送ること。"""
        terminal, manager, sent = self._session()
        self._type(terminal, "show version\r")
        del sent[:]

        manager._send_keepalive("dev")

        self.assertEqual(sent, ["\r"], "キープアライブが止まったままになった")

    def test_ctrl_c_and_ctrl_u_release_the_half_typed_line(self):
        """行を捨てる Ctrl+C / Ctrl+U の後は、キープアライブを送ること。"""
        from PyQt6.QtCore import Qt
        for name, key, ch in (("Ctrl+C", Qt.Key.Key_C, "\x03"),
                              ("Ctrl+U", Qt.Key.Key_U, "\x15")):
            with self.subTest(name):
                terminal, manager, sent = self._session()
                self._type(terminal, "reload")
                self._control_key(terminal, key, ch)
                self.assertEqual(sent[-1], ch, "前提: 制御文字を送っている")
                del sent[:]

                manager._send_keepalive("dev")

                self.assertEqual(sent, ["\r"],
                                 "%s で行を捨てた後も送らないままだった" % name)

    def test_a_backspace_leaves_the_line_half_typed(self):
        """Backspace は行が残っているか分からないので、送らない側に倒すこと。"""
        terminal, manager, sent = self._session()
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        self._type(terminal, "show version\r")
        QTest.keyClick(terminal, Qt.Key.Key_Backspace)
        del sent[:]

        manager._send_keepalive("dev")

        self.assertEqual(sent, [], "判断できない操作の後に送ってしまった: %r" % sent)

    def test_a_pasted_line_without_a_newline_holds_the_keepalive(self):
        """改行なしで貼り付けた行の後ろにも、キープアライブの CR が付かないこと。"""
        terminal, manager, sent = self._session()
        terminal.send_text("write erase")
        del sent[:]

        manager._send_keepalive("dev")

        self.assertEqual(sent, [], "貼り付けた行が実行された: %r" % sent)

    def test_an_ime_commit_holds_the_keepalive(self):
        """IME で確定した文字も打ちかけとして数えること。"""
        terminal, manager, sent = self._session()
        self._commit(terminal, "設定")
        del sent[:]

        manager._send_keepalive("dev")

        self.assertEqual(sent, [], "IME の確定文字の後に送ってしまった: %r" % sent)

    def test_macro_sends_do_not_count_as_a_half_typed_line(self):
        """マクロの送信は打ちかけの判断に数えないこと。"""
        terminal, manager, sent = self._session()
        terminal.queue_macro_send("show version")   # 行送りを付けずに積む
        del sent[:]

        manager._send_keepalive("dev")

        self.assertEqual(sent, ["\r"],
                         "マクロの送信をきっかけにキープアライブが止まった")

    def test_reconnecting_releases_the_half_typed_line(self):
        """接続し直したら、打ちかけは解けること。"""
        terminal, manager, sent = self._session()
        self._type(terminal, "reload")
        terminal.set_reconnect_mode(True)
        terminal.set_reconnect_mode(False)
        del sent[:]

        manager._send_keepalive("dev")

        self.assertEqual(sent, ["\r"], "再接続しても送らないままだった")


if __name__ == "__main__":
    unittest.main()
