"""送れない文字（孤立したサロゲート）を含む IME の確定・打鍵を、区切りに分ける前に丸ごと断ることを検証する。

何が起きていたか（ad7caa5 の後）。送れない文字を丸ごと断る点検は、貼り付けの
入口 InteractiveTerminal.send_text にだけあった。IME の確定文字列
（inputMethodEvent）と打鍵の文字（keyPressEvent）は _send_typed から送信列へ
そのまま積まれ、点検を通らずに接続へ渡っていた（検査役の ime_surrogate.py で
実測: 'show' + U+D800 + 'x' がそのまま key_pressed へ出た）。受ける側は接続の
send_command の保険で、区切り（SEND_CHUNK = 512 文字）1 つだけを断って
続ける。確定が 512 文字を超えると、送れない文字より前の区切りは機器へ届き、
その後ろの区切りも続けて届くので、行の途中が抜けたまま機器に残る
（決定 (a)「途中まで送らない」に反する）。

どう直したか。_send_typed の入口で同じ点検をし、送れない文字を含むなら
何も積まずに、貼り付けと同じ文言で理由（どの文字が何文字目か）を出す。
これで利用者の入力（貼り付け・打鍵・IME）は、どれも区切りに分ける前に
丸ごと断られ、接続の保険が 1 つの入力の途中で働くことは無くなる。
マクロと自動実行コマンドは読み込み時の隔離で扱う（GUI からは保存できない）。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, "src")

SUR = chr(0xD800)
NOTICE = "送れない文字"


class TypedUnsendableRefusedWholeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        term = widget.create_terminal_tab("dev")
        term.set_input_enabled(True)
        handed, notices = [], []
        term.key_pressed.connect(handed.append)
        term.notice_requested.connect(notices.append)
        return term, handed, notices

    def _commit(self, term, text):
        from PyQt6.QtGui import QInputMethodEvent
        event = QInputMethodEvent()
        event.setCommitString(text)
        term.inputMethodEvent(event)
        self._pump(0.3)

    def test_a_long_ime_commit_is_refused_before_any_chunk_is_handed(self):
        from ui.terminal_widget import InteractiveTerminal
        term, handed, notices = self._terminal()
        text = "a" * 600 + SUR + "b" * 10
        self.assertGreater(len(text), InteractiveTerminal.SEND_CHUNK,
                           "前提: 確定が区切りより長い")

        self._commit(term, text)

        self.assertEqual([], handed,
                         "送れない文字より前の区切りを接続へ渡した（途中まで送られる）")
        self.assertEqual(1, len(notices), "理由を知らせていない: %r" % notices)
        self.assertIn(NOTICE, notices[0])
        self.assertIn("U+D800", notices[0])
        self.assertIn("601 文字目", notices[0])

    def test_a_short_ime_commit_is_refused(self):
        term, handed, notices = self._terminal()

        self._commit(term, "show" + SUR + "x")

        self.assertEqual([], handed)
        self.assertEqual(1, len(notices))
        self.assertIn("5 文字目", notices[0])

    def test_a_key_press_is_refused(self):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        term, handed, notices = self._terminal()

        term.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                                     Qt.KeyboardModifier.NoModifier, SUR))
        self._pump(0.3)

        self.assertEqual([], handed)
        self.assertEqual(1, len(notices))

    def test_ordinary_input_still_goes_out(self):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        term, handed, notices = self._terminal()

        self._commit(term, "日本語" + "a" * 600)
        term.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_B,
                                     Qt.KeyboardModifier.NoModifier, "b"))
        self._pump(0.3)

        self.assertEqual("日本語" + "a" * 600 + "b", "".join(handed))
        self.assertEqual([], notices)


if __name__ == "__main__":
    unittest.main()
