"""接続が落ちたら、送りかけの貼り付けを捨てることを検証する。

貼り付けは 512 文字ずつのキューで送る。切断されるとタブは再接続待ちに
なるが、_send_queue と _sending はそのまま残っていた。再接続でも同じ
InteractiveTerminal を使い回し、_attach_screen はパーサと画面しか
張り直さないので、残っていたチャンクは新しい接続の key_pressed へ
そのまま流れる（実測: 直列に再接続すると残り 1708 文字が新セッションへ
届いた）。

実アプリでは Enter を押すまで再接続しないため、人がその頃にはキューは
旧接続へ捨てられて空になっている。ただし状態の掃除としては穴なので、
切断で再接続待ちに入った時点でキューを捨て、待ちの間は排出しない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class SendQueueOnDisconnectTest(unittest.TestCase):
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

    def _pump(self, times=20):
        for _ in range(times):
            self.app.processEvents()

    def test_a_disconnect_mid_paste_drops_what_was_still_queued(self):
        """再接続待ちに入ったら、残りのチャンクを送らないこと。"""
        w, terminal, sent = self._terminal()
        text = "x" * (terminal.SEND_CHUNK * 4 + 100)

        terminal.send_text(text)
        first = "".join(sent)
        self.assertEqual(len(first), terminal.SEND_CHUNK,
                         "前提: 最初のひと区切りだけが同期で送られる")

        # 切断 → 再接続待ち（main_window の enable_reconnect と同じ経路）
        w.enable_reconnect("dev", lambda name: None)
        self._pump()

        self.assertEqual("".join(sent), first,
                         "再接続待ちの間に残りのチャンクが送られている")
        self.assertEqual(terminal._send_queue, [],
                         "捨てたはずのキューが残っている")

    def test_nothing_leaks_into_the_session_after_reconnect(self):
        """再接続後の新しい接続へ、前の貼り付けの残りが流れないこと。"""
        w, terminal, sent = self._terminal()
        terminal.send_text("y" * (terminal.SEND_CHUNK * 3))
        w.enable_reconnect("dev", lambda name: None)

        # 再接続: 同じタブを再利用し、繋がったところで待ちが解ける
        # （解くのは MainWindow._on_connection_success）
        w.create_terminal_tab("dev")
        terminal.set_reconnect_mode(False)
        after = []
        terminal.key_pressed.connect(after.append)
        self._pump()

        self.assertEqual(after, [],
                         "前のセッションの貼り付けが新しい接続へ流れている")

    def test_a_paste_after_reconnect_still_goes_through(self):
        """再接続後の新しい貼り付きは、これまでどおり送られること。"""
        w, terminal, sent = self._terminal()
        terminal.send_text("z" * (terminal.SEND_CHUNK * 2))
        w.enable_reconnect("dev", lambda name: None)
        w.create_terminal_tab("dev")
        terminal.set_reconnect_mode(False)   # 接続成功で待ちが解ける
        self._pump()
        del sent[:]

        terminal.send_text("show version")
        self._pump()

        self.assertEqual("".join(sent), "show version")


if __name__ == "__main__":
    unittest.main()
