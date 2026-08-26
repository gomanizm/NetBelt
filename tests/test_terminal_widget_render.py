"""新しい描画経路 (パーサ + 画面モデル + QTextEdit) のウィジェット検証。

v1.1.1 まではカーソル位置指定を解釈できず、nano や vi は 1 行に潰れ、
「対応していません」の案内を出すのが精一杯だった。v1.2.0 は画面を
持ったので、案内ではなく実際に描く。ここでは旧実装の検証で守って
いた性質のうち、残るべきものを新実装の言葉で固定する。

文書の構造は [確定した記録] + [今の画面]。記録は二度と書き換えない。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, "src")

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures")


def fixture(name):
    raw = io.open(os.path.join(FIXTURES, name), "rb").read()
    return raw.decode("utf-8", "replace")


class WidgetRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        return w

    def screen_text(self, w):
        return w._terminals["dev"].toPlainText()


class NanoRendersForRealTest(WidgetRenderTest):
    """実機の nano (527 バイト)。旧実装では 1 行に潰れていた。"""

    def setUp(self):
        self.w = self.widget()
        self.w.append_output("dev", "user@lab:~$ ")
        self.w.append_output("dev", fixture("nano_vt100.bin"))
        self.lines = self.screen_text(self.w).split("\n")

    def test_the_editor_layout_is_actually_drawn(self):
        self.assertTrue(any("GNU nano 7.2" in l for l in self.lines))
        self.assertTrue(self.lines[-2].startswith("^G Help"))
        self.assertTrue(self.lines[-1].startswith("^X Exit"))

    def test_no_apology_is_shown_any_more(self):
        self.assertNotIn("対応していません", self.screen_text(self.w))

    def test_the_record_before_nano_survives(self):
        self.assertTrue(any("user@lab:~$" in l for l in self.lines))


class ClearKeepsTheSessionTest(WidgetRenderTest):
    """実機の clear (255 バイト)。記録を失わないこと。"""

    def test_what_was_on_screen_moves_into_the_record(self):
        w = self.widget()
        w.append_output("dev", "user@lab:~$ ls\r\nfile1  file2\r\n")
        w.append_output("dev", fixture("clear_vt100.bin"))
        text = self.screen_text(w)
        self.assertIn("file1  file2", text)
        self.assertTrue(text.rstrip().endswith("user@lab:~$"))


class SplitInvarianceTest(WidgetRenderTest):
    """受信がどこで切れても同じ画面になること。切れ目対応は
    パーサが状態として持つので、ウィジェットに継ぎ足し処理は無い。"""

    def render_split(self, raw, cut):
        w = self.widget()
        w.append_output("dev", raw[:cut])
        w.append_output("dev", raw[cut:])
        return self.screen_text(w)

    def test_every_split_point_of_nano_renders_the_same(self):
        raw = fixture("nano_vt100.bin")
        w = self.widget()
        w.append_output("dev", raw)
        whole = self.screen_text(w)
        for cut in range(1, len(raw)):
            if self.render_split(raw, cut) != whole:
                self.fail("%d バイト目で切ると画面が変わる" % cut)

    def test_a_window_title_never_leaks_at_any_split(self):
        raw = "before\x1b]0;user@lab: /home/secret\x07after"
        for cut in range(1, len(raw)):
            text = self.render_split(raw, cut)
            self.assertNotIn("secret", text, "%d バイト目" % cut)
            self.assertIn("beforeafter", text)


class ReconnectTest(WidgetRenderTest):
    def test_terminal_state_starts_over_but_the_record_stays(self):
        w = self.widget()
        w.append_output("dev", "old session\r\n\x1b[?1h")
        self.assertTrue(w._terminals["dev"]._screen.application_cursor_keys)
        w.create_terminal_tab("dev")        # 再接続はタブを使い回す
        self.assertFalse(w._terminals["dev"]._screen.application_cursor_keys)
        w.append_output("dev", "new session")
        text = self.screen_text(w)
        self.assertIn("old session", text)
        self.assertIn("new session", text)


class SelectionSurvivesOutputTest(WidgetRenderTest):
    def test_incoming_output_does_not_break_a_selection(self):
        # コピーしようと選択している最中に機器がログを吐いても、
        # 選択が消えたり置き換わったりしないこと (旧実装から守る性質)
        from PyQt6.QtGui import QTextCursor
        w = self.widget()
        w.append_output("dev", "pick me\r\n")
        terminal = w._terminals["dev"]
        cursor = terminal.textCursor()
        cursor.setPosition(0)
        cursor.movePosition(QTextCursor.MoveOperation.Right,
                            QTextCursor.MoveMode.KeepAnchor, 7)
        terminal.setTextCursor(cursor)
        w.append_output("dev", "more output\r\n")
        self.assertEqual(terminal.textCursor().selectedText(), "pick me")
        self.assertIn("more output", self.screen_text(w))


class DeviceQueryTest(WidgetRenderTest):
    def test_a_cursor_position_query_is_answered(self):
        w = self.widget()
        sent = []
        w._terminals["dev"].key_pressed.connect(sent.append)
        w.append_output("dev", "ab\x1b[6n")
        self.assertEqual(sent, ["\x1b[1;3R"])


if __name__ == "__main__":
    unittest.main()
