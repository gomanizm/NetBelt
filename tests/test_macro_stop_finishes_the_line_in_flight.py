"""マクロ停止が、送りかけの1行を途中で切らないことを検証する。

マクロの1行は SEND_CHUNK（512 文字）ずつに割って送る。行がこれを
超えていると、停止したときに先頭の 512 文字だけが機器へ届き、残りと
行末の CR が取り消される。機器の入力行には中途半端な文字列が残り、
次に利用者が打った文字がその続きになる。

停止で取り消すのは「まだ1文字も送っていないマクロ由来の断片」だけに
する。送り始めてしまった1行は最後まで送り切り、行として閉じさせる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class MacroStopFinishesTheLineInFlightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("dev")
        terminal = w._terminals["dev"]
        terminal.set_input_enabled(True)
        sent = []
        terminal.key_pressed.connect(sent.append)
        return w, terminal, sent

    def _pump(self, times=40):
        for _ in range(times):
            self.app.processEvents()

    def test_a_long_line_already_started_is_sent_to_its_end(self):
        """排出中の1行は、停止しても残りと CR まで届くこと。"""
        w, terminal, sent = self._terminal()
        chunk = terminal.SEND_CHUNK
        line = "C" * (chunk * 2) + "\r"

        terminal.queue_macro_send(line)
        self.assertEqual(sent, ["C" * chunk],
                         "前提: 最初のひと区切りだけが同期で送られる")

        terminal.cancel_macro_sends()
        self._pump()

        self.assertEqual("".join(sent), line,
                         "送りかけの行が途中で切られた: 送れたのは %d 文字 / "
                         "CR は %r" % (len("".join(sent)),
                                       "".join(sent).endswith("\r")))

    def test_a_line_that_never_started_is_still_dropped(self):
        """まだ1文字も送っていない行は、これまでどおり取り消すこと。"""
        w, terminal, sent = self._terminal()
        chunk = terminal.SEND_CHUNK
        first = "C" * (chunk * 2) + "\r"

        terminal.queue_macro_send(first)
        terminal.queue_macro_send("show version\r")
        terminal.cancel_macro_sends()
        self._pump()

        self.assertEqual("".join(sent), first,
                         "手付かずのマクロ行まで送られた: %r"
                         % ["".join(sent)[len(first):]])

    def test_a_short_line_not_yet_started_is_dropped(self):
        """貼り付けの後ろで待っている短い行は、取り消せること。"""
        w, terminal, sent = self._terminal()
        chunk = terminal.SEND_CHUNK
        terminal.send_text("P" * (chunk * 2))
        self.assertEqual(sent, ["P" * chunk],
                         "前提: 貼り付けの最初のひと区切りだけが送られる")

        terminal.queue_macro_send("cmd1\r")
        terminal.cancel_macro_sends()
        self._pump()

        self.assertEqual("".join(sent), "P" * (chunk * 2),
                         "停止したのにマクロのコマンドが届いた: %r"
                         % "".join(sent)[chunk * 2:])


if __name__ == "__main__":
    unittest.main()
