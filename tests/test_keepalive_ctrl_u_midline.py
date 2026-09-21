"""行の途中で押した Ctrl+U が、打ちかけの印を解いてしまわないことを検証する。

何が起きていたか（実測: scratchpad\\cx5j-verify-termui\\t01_ctrl_u_midline.py）:
端末で 'echo CHECK' を打って Ctrl+A（0x01、行頭へ）→ Ctrl+U（0x15）と押した
あと、MacroManager._send_keepalive('dev') が CR を送っていた。'reload' を打って
左矢印（ESC[D）を 2 回押してから Ctrl+U の場合も同じ。
    case1 sent(all)= ['e','c','h','o',' ','C','H','E','C','K','\\x01','\\x15']
    case1 _typing_unsent after ctrl+u = False
    case1 keepalive sent = ['\\r']
Ctrl+U が LINE_ENDERS に入っていたため、Ctrl+U 単独の送信では
`cut < len(payload) - 1` が必ず False になり、直前に矢印キーが立てていた
打ちかけの印まで解けていた。機器側の Ctrl+U は「カーソルからコマンド行の
先頭まで」を消す操作（bash の unix-line-discard、Cisco IOS も同じ）なので、
行頭へ移ってから押すと機器には全文が残り、続く CR で 'echo CHECK' が、
左矢印 2 回のときは 'ad' が実行される。

利用者の決定（2026-09-20）: 安全側に倒す。Ctrl+U は「行が残っているか判断
できない操作」として扱い、打ちかけの印を解かない（Backspace と同じ扱い）。
Enter と Ctrl+C は今までどおり解く。

どう直したか: InteractiveTerminal.LINE_ENDERS から "\\x15" を外した。
Ctrl+U を送ると行送りが見つからないので打ちかけのままになり、その回の
キープアライブは飛ばされる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class KeepaliveCtrlUMidLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _session(self):
        """入力できる端末と、そこへ登録した MacroManager を作る。

        tests/test_keepalive_skips_half_typed_line.py と同じ組み方。
        """
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
        manager.register_send_callback(
            "dev", terminal._queue_send,
            command_callback=terminal.queue_macro_send,
            cancel_callback=terminal.cancel_macro_sends)
        self.addCleanup(manager.unregister_send_callback, "dev")
        return terminal, manager, sent

    @staticmethod
    def _type(terminal, text):
        """利用者が 1 文字ずつ打鍵したのと同じ経路を通す。"""
        from PyQt6.QtTest import QTest
        for ch in text:
            QTest.keyClicks(terminal, ch)

    @staticmethod
    def _control_key(terminal, key, char):
        """Ctrl + 英字の打鍵を起こす。

        QTest.keyClick に ControlModifier を付けても text は英字のままで
        制御文字にならないので、実際の Qt がくれるイベントを組み立てる。
        """
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        terminal.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, key,
            Qt.KeyboardModifier.ControlModifier, char))

    def test_ctrl_u_at_the_start_of_the_line_holds_the_keepalive(self):
        """Ctrl+A で行頭へ移ってからの Ctrl+U では、CR を送らないこと。

        機器側には 'echo CHECK' がそのまま残っているので、ここで CR を
        送ると利用者が押していないコマンドが実行される。
        """
        from PyQt6.QtCore import Qt
        terminal, manager, sent = self._session()
        self._type(terminal, "echo CHECK")
        self._control_key(terminal, Qt.Key.Key_A, "\x01")
        self._control_key(terminal, Qt.Key.Key_U, "\x15")
        self.assertEqual(sent[-2:], ["\x01", "\x15"],
                         "前提: 制御文字を送っている")
        del sent[:]

        manager._send_keepalive("dev")       # タイマーの発火と同じ

        self.assertEqual(sent, [],
                         "行頭での Ctrl+U のあとに CR を送った: %r" % sent)

    def test_ctrl_u_after_the_left_arrow_holds_the_keepalive(self):
        """左矢印で戻ってからの Ctrl+U でも、CR を送らないこと。

        'reload' の途中まで戻って Ctrl+U を押すと機器側には 'ad' が残る。
        """
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        terminal, manager, sent = self._session()
        self._type(terminal, "reload")
        QTest.keyClick(terminal, Qt.Key.Key_Left)
        QTest.keyClick(terminal, Qt.Key.Key_Left)
        self._control_key(terminal, Qt.Key.Key_U, "\x15")
        self.assertEqual(sent[-1], "\x15", "前提: 制御文字を送っている")
        del sent[:]

        manager._send_keepalive("dev")

        self.assertEqual(sent, [],
                         "左矢印のあとの Ctrl+U で CR を送った: %r" % sent)

    def test_enter_still_releases_the_half_typed_line(self):
        """Enter は今までどおり打ちかけを解くこと（この変更で壊さない）。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        terminal, manager, sent = self._session()
        self._type(terminal, "show version")
        QTest.keyClick(terminal, Qt.Key.Key_Return)
        del sent[:]

        manager._send_keepalive("dev")

        self.assertEqual(sent, ["\r"], "キープアライブが止まったままになった")


if __name__ == "__main__":
    unittest.main()
