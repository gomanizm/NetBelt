"""ターミナルへのドロップと Ctrl+ホイールが、既存の経路を通ることを検証する。

どちらも Qt の既定動作がそのまま露出していた。

  ドロップ  : 落としたテキストが画面へ直接挿入され、機器は何も受け取って
              いないのに入力済みに見える（実測で確認）。
  Ctrl+ホイール: QTextEdit の組込みズームが働き、config へ保存されず、
              6〜32pt の制限も受けず、設定を再適用すると失われる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class TerminalDropTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self, connected=True):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        terminal = w._terminals["dev"]
        terminal.set_input_enabled(connected)
        w.append_output("dev", "Router#")
        return w, terminal

    def _drop(self, terminal, text):
        from PyQt6.QtCore import QMimeData
        mime = QMimeData()
        mime.setText(text)
        sent = []
        terminal.key_pressed.connect(sent.append)
        terminal.insertFromMimeData(mime)
        terminal.key_pressed.disconnect()
        return sent

    def test_dropped_text_goes_to_the_device(self):
        """落としたテキストは機器へ送られること。"""
        w, terminal = self._terminal()
        sent = self._drop(terminal, "show version")
        self.assertEqual("".join(sent), "show version")

    def test_dropped_text_is_not_written_straight_to_the_screen(self):
        """機器を経由せず画面へ書かないこと。

        画面に出てよいのは機器が返したエコーだけ。直接書くと、機器が
        受け取っていないコマンドが入力済みに見える。
        """
        w, terminal = self._terminal()
        before = terminal.toPlainText()
        self._drop(terminal, "reload")
        self.assertEqual(terminal.toPlainText(), before,
                         "機器を経由せず画面へ文字が入った")

    def test_a_drop_on_a_disconnected_tab_sends_nothing(self):
        """接続していないタブでは何も起きないこと。"""
        w, terminal = self._terminal(connected=False)
        before = terminal.toPlainText()
        sent = self._drop(terminal, "show version")
        self.assertEqual(sent, [])
        self.assertEqual(terminal.toPlainText(), before)

    def test_newlines_in_a_drop_are_sent_as_carriage_returns(self):
        """複数行を落としたとき、改行は端末と同じ CR で送ること。"""
        w, terminal = self._terminal()
        sent = self._drop(terminal, "conf t\nhostname R1\n")
        self.assertEqual("".join(sent), "conf t\rhostname R1\r")


class TerminalWheelTest(unittest.TestCase):
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

    def _wheel(self, terminal, up=True, ctrl=True):
        from PyQt6.QtCore import Qt, QPoint, QPointF
        from PyQt6.QtGui import QWheelEvent
        mods = (Qt.KeyboardModifier.ControlModifier if ctrl
                else Qt.KeyboardModifier.NoModifier)
        return QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0),
                           QPoint(0, 120 if up else -120),
                           Qt.MouseButton.NoButton, mods,
                           Qt.ScrollPhase.NoScrollPhase, False)

    def test_ctrl_wheel_asks_for_a_font_size_change(self):
        """Ctrl+ホイールは要求を上へ投げること（自分でズームしない）。"""
        w, terminal = self._terminal()
        got = []
        terminal.font_size_change_requested.connect(got.append)

        terminal.wheelEvent(self._wheel(terminal, up=True))
        terminal.wheelEvent(self._wheel(terminal, up=False))

        self.assertEqual(got, [1, -1])

    def test_ctrl_wheel_does_not_zoom_the_widget_itself(self):
        """組込みズームが働かないこと。

        働くと config を通らず、6〜32pt の制限も受けず、設定を
        再適用した拍子に消える。
        """
        w, terminal = self._terminal()
        w.apply_terminal_settings({"font_size": 10, "font_family": "Consolas",
                                   "background_color": "#000000",
                                   "text_color": "#FFFFFF"})
        before = terminal.document().defaultFont().pointSize()

        for _ in range(5):
            terminal.wheelEvent(self._wheel(terminal, up=True))

        self.assertEqual(terminal.document().defaultFont().pointSize(), before,
                         "ウィジェットが自分でズームしている")

    def test_a_plain_wheel_still_scrolls(self):
        """Ctrl なしのホイールは、これまでどおりスクロールに使えること。"""
        w, terminal = self._terminal()
        got = []
        terminal.font_size_change_requested.connect(got.append)

        terminal.wheelEvent(self._wheel(terminal, up=True, ctrl=False))

        self.assertEqual(got, [], "Ctrl なしでフォントサイズを変えている")

    def test_the_widget_relays_the_request(self):
        """TerminalWidget が要求を中継すること（MainWindow が受け取る経路）。"""
        w, terminal = self._terminal()
        got = []
        w.font_size_change_requested.connect(got.append)

        terminal.wheelEvent(self._wheel(terminal, up=True))

        self.assertEqual(got, [1])


if __name__ == "__main__":
    unittest.main()
