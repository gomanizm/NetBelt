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

    def widget(self, rows=None, cols=None):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        if rows is not None:
            w._grid_size = lambda t, r=rows, c=cols: (r, c)
        w.create_terminal_tab("dev")
        if rows is not None:
            w._apply_grid_size("dev")
        return w

    def visible_screen(self, w):
        """いま端末に映っている行 (文書の末尾 rows 行)。"""
        terminal = w._terminals["dev"]
        return terminal.toPlainText().split("\n")[-terminal._screen.rows:]

    def screen_text(self, w):
        return w._terminals["dev"].toPlainText()


class NanoRendersForRealTest(WidgetRenderTest):
    """実機の nano (527 バイト)。旧実装では 1 行に潰れていた。"""

    def setUp(self):
        # 採取したときと同じ 24x80 で描く。nano は端末の大きさに合わせて
        # 組むので、違う大きさで流すと下端の位置が合わない
        self.w = self.widget(24, 80)
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


class ScreenOccupiesTheViewportTest(WidgetRenderTest):
    """画面は必ず行数ぶんの高さを占めること。

    実機試験で「clear が効かない」「nano の表示がおかしい」と報告された。
    どちらも原因は同じで、末尾の空行を詰めていたために履歴が下端へ
    せり上がり、画面が窓の一部しか占めていなかった。
    """

    def test_clear_leaves_an_empty_screen(self):
        w = self.widget(24, 80)
        w.append_output("dev", "\r\n".join("out-%02d" % i for i in range(20))
                        + "\r\n$ clear\r\n")
        w.append_output("dev", fixture("clear_vt100.bin"))
        screen = self.visible_screen(w)
        self.assertEqual(len(screen), 24)
        self.assertFalse([l for l in screen if "out-" in l],
                         "clear のあとも直前の出力が画面に残っている")
        self.assertIn("user@lab:~$", "".join(screen))
        self.assertIn("out-19", self.screen_text(w))    # 記録には残る

    def test_a_full_screen_app_fills_the_screen(self):
        w = self.widget(24, 80)
        w.append_output("dev", "\r\n".join("shell-%02d" % i for i in range(20))
                        + "\r\n$ nano\r\n")
        w.append_output("dev", fixture("nano_vt100.bin"))
        screen = self.visible_screen(w)
        self.assertEqual(len(screen), 24)
        self.assertIn("GNU nano", screen[0])
        self.assertTrue(screen[-1].startswith("^X Exit"))
        self.assertFalse([l for l in screen if l.startswith("shell-")],
                         "画面にシェルの履歴が混ざっている")

    def test_the_view_stays_at_the_bottom(self):
        w = self.widget(24, 80)
        w.append_output("dev", "\r\n".join("l%02d" % i for i in range(60)))
        bar = w._terminals["dev"].verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum())


class ResizeKeepsContentTest(WidgetRenderTest):
    """窓を縮めても一文字も失わないこと (実機試験で報告)。"""

    KEY = "ssh-ed25519 " + "A" * 68 + " user@example.com"

    def test_a_narrow_spell_leaves_no_chopped_lines_behind(self):
        """窓を戻したら、刻まれた行が残らないこと。

        実機試験で「ls /etc/ のあと窓を縮めて戻すと表示が崩れ、
        以後のコマンドにも残る」と報告された。狭い間に履歴へ流れた
        行も繋がっていなければならない。
        """
        long_line = "x" * 100
        w = self.widget(24, 40)
        w.append_output("dev", "\r\n".join([long_line] * 30) + "\r\n$ ")
        self.assertGreater(len(w._terminals["dev"]._screen.history), 0,
                           "履歴へ流れていない (前提が崩れている)")
        w._grid_size = lambda t: (24, 100)
        w._apply_grid_size("dev")

        printed = [l for l in self.screen_text(w).split("\n") if l.strip()]
        chopped = [l for l in printed
                   if l != long_line and l.rstrip() != "$"]
        self.assertEqual(chopped, [], "刻まれた行が残っている")
        self.assertEqual(printed.count(long_line), 30)

    def test_a_long_line_survives_repeated_shrinking(self):
        w = self.widget(24, 120)
        w.append_output("dev", "$ cat ~/.ssh/authorized_keys\r\n"
                        + self.KEY + "\r\n$ ")
        for cols in (100, 80, 60, 40, 30):
            w._grid_size = lambda t, c=cols: (24, c)
            w._apply_grid_size("dev")
            joined = "".join(self.screen_text(w).split("\n"))
            self.assertIn(self.KEY.replace(" ", ""),
                          joined.replace(" ", ""),
                          "%d 桁へ縮めたときに欠けた" % cols)


class NoticeTest(WidgetRenderTest):
    """アプリ自身の案内が階段状にならないこと。

    端末の LF は「1 行下へ」で行頭には戻らない。アプリの文言は普通の
    改行で書かれているので、そのまま流すと切断バナーが桁ずれする。
    """

    def test_a_banner_stays_left_aligned(self):
        w = self.widget(24, 80)
        w.append_output("dev", "lab-rtr#")       # プロンプト表示中
        w.show_notice("dev",
                      "\n\n====\nセッションが切断されました\n====\n")
        printed = [l for l in self.screen_text(w).split("\n") if l.strip()]
        for line in printed[1:]:
            self.assertFalse(line.startswith(" "),
                             "案内が字下げされている: %r" % line)

    def test_device_output_is_untouched(self):
        # 機器からの LF は端末の意味のまま扱う (show の桁揃えが崩れる)
        w = self.widget(24, 80)
        w.append_output("dev", "abc\ndef")
        self.assertEqual(self.screen_text(w).split("\n")[1], "   def")


class LogRecordingTest(WidgetRenderTest):
    def test_tabs_are_kept_in_the_log(self):
        """タブは桁を作る文字なので、落とすと表が潰れる。"""
        import io as _io
        import tempfile
        w = self.widget(24, 80)
        path = os.path.join(tempfile.gettempdir(), "netbelt_tab_log.txt")
        w._log_files["dev"] = _io.open(path, "w", encoding="utf-8")
        try:
            w.append_output("dev", "Interface\tStatus\r\nGi0/0\tup\r\n")
            w._log_files["dev"].close()
            self.assertEqual(_io.open(path, encoding="utf-8").read(),
                             "Interface\tStatus\nGi0/0\tup\n")
        finally:
            os.remove(path)


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
    def test_a_selection_survives_the_screen_scrolling(self):
        """画面がスクロールしても範囲選択が消えないこと。

        v1.1.0 で直した性質。画面が 1 行上がると全行がずれるので、
        素朴に描き直すと選択が Qt に潰される。show run をコピーしよう
        としている最中に機器が syslog を 1 行吐くだけで起きる。
        """
        from PyQt6.QtGui import QTextCursor
        w = self.widget()
        terminal = w._terminals["dev"]
        rows = terminal._screen.rows
        w.append_output("dev",
                        "\r\n".join("line %02d" % i for i in range(rows + 8)))
        self.assertGreater(len(terminal._screen.history), 0,
                           "スクロールが起きていない (前提が崩れている)")

        for word in ("line 05", "line 12", "line %02d" % (rows + 5)):
            text = terminal.toPlainText()
            cursor = terminal.textCursor()
            cursor.setPosition(text.index(word))
            cursor.movePosition(QTextCursor.MoveOperation.Right,
                                QTextCursor.MoveMode.KeepAnchor, len(word))
            terminal.setTextCursor(cursor)
            w.append_output("dev", "\r\nsyslog message")
            self.assertEqual(terminal.textCursor().selectedText(), word,
                             "%s の選択が消えた" % word)

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


class ColourTest(WidgetRenderTest):
    """SGR が実際の色になること。番号→実色の対応は xterm の既定値。"""

    def format_at(self, w, needle):
        from PyQt6.QtGui import QTextCursor
        terminal = w._terminals["dev"]
        pos = terminal.toPlainText().index(needle)
        cursor = QTextCursor(terminal.document())
        cursor.setPosition(pos + 1)         # charFormat は直前の文字の書式
        return cursor.charFormat()

    def test_basic_colours_reach_the_glyphs(self):
        w = self.widget()
        w.append_output("dev", "\x1b[31mRED\x1b[m plain")
        self.assertEqual(self.format_at(w, "RED").foreground().color().name(),
                         "#cd0000")

    def test_default_text_carries_no_explicit_colour(self):
        # 書式を空に戻しておかないと、設定でパレットを変えたとき
        # 過去の文字だけ古い色で残ってしまう
        from PyQt6.QtGui import QTextFormat
        w = self.widget()
        w.append_output("dev", "\x1b[31mRED\x1b[m plain")
        self.assertFalse(self.format_at(w, "plain").hasProperty(
            QTextFormat.Property.ForegroundBrush))

    def test_reverse_video_swaps_the_palette(self):
        w = self.widget()
        w.append_output("dev", "\x1b[7m--More--\x1b[m")
        fmt = self.format_at(w, "--More--")
        settings = w._terminal_settings
        self.assertEqual(fmt.foreground().color().name(),
                         settings["background_color"].lower())
        self.assertEqual(fmt.background().color().name(),
                         settings["text_color"].lower())

    def test_bold_is_bold(self):
        from PyQt6.QtGui import QFont
        w = self.widget()
        w.append_output("dev", "\x1b[1mboot system\x1b[m")
        self.assertEqual(self.format_at(w, "boot").fontWeight(),
                         QFont.Weight.Bold)

    def test_the_256_colour_cube(self):
        w = self.widget()
        w.append_output("dev", "\x1b[38;5;196mX")
        self.assertEqual(self.format_at(w, "X").foreground().color().name(),
                         "#ff0000")

    def test_colour_survives_scrolling_into_the_record(self):
        w = self.widget()
        w.append_output("dev", "\x1b[32mUP\x1b[m\r\n" + "\r\n" * 30)
        self.assertEqual(self.format_at(w, "UP").foreground().color().name(),
                         "#00cd00")

    def test_nano_title_bar_keeps_its_band(self):
        # 反転属性の付いた空白は帯として意味があるので、行末でも
        # 削らずに描く
        w = self.widget()
        w.append_output("dev", fixture("nano_vt100.bin"))
        title = [l for l in w._terminals["dev"].toPlainText().split("\n")
                 if "GNU nano" in l][0]
        self.assertEqual(len(title), 80)
        fmt = self.format_at(w, "GNU nano")
        self.assertEqual(fmt.background().color().name(), "#ffffff")


class GridResizeTest(WidgetRenderTest):
    """ウィンドウの大きさに格子が追従し、機器へ知らせること。"""

    def test_a_new_size_reaches_screen_and_signal(self):
        w = self.widget()
        recorded = []
        w.terminal_resized.connect(
            lambda d, c, r: recorded.append((d, c, r)))
        w._grid_size = lambda terminal: (30, 100)
        w._apply_grid_size("dev")
        screen = w._terminals["dev"]._screen
        self.assertEqual((screen.rows, screen.cols), (30, 100))
        self.assertEqual(recorded, [("dev", 100, 30)])

    def test_the_same_size_is_not_re_announced(self):
        w = self.widget()
        recorded = []
        w.terminal_resized.connect(
            lambda d, c, r: recorded.append((d, c, r)))
        w._grid_size = lambda terminal: (30, 100)
        w._apply_grid_size("dev")
        w._apply_grid_size("dev")
        self.assertEqual(len(recorded), 1)

    def test_what_was_written_survives_the_resize(self):
        w = self.widget()
        w.append_output("dev", "show run\r\nhostname lab-rtr\r\n")
        w._grid_size = lambda terminal: (10, 40)
        w._apply_grid_size("dev")
        self.assertIn("hostname lab-rtr", self.screen_text(w))

    def test_grid_size_stays_within_sane_bounds(self):
        w = self.widget()
        rows, cols = w._grid_size(w._terminals["dev"])
        self.assertTrue(5 <= rows <= 200, rows)
        self.assertTrue(20 <= cols <= 500, cols)


class InputModeTest(WidgetRenderTest):
    """機器が立てた入力モードに、送る側が合わせること。"""

    def sendable(self, w):
        terminal = w._terminals["dev"]
        terminal.set_input_enabled(True)
        return terminal

    def test_arrow_keys_follow_decckm(self):
        w = self.widget()
        terminal = self.sendable(w)
        self.assertEqual(terminal._cursor_key("A"), "\x1b[A")
        w.append_output("dev", "\x1b[?1h")      # nano が立てる
        self.assertEqual(terminal._cursor_key("A"), "\x1bOA")
        w.append_output("dev", "\x1b[?1l")
        self.assertEqual(terminal._cursor_key("A"), "\x1b[A")

    def test_a_paste_is_bracketed_only_when_asked(self):
        w = self.widget()
        terminal = self.sendable(w)
        sent = []
        terminal.key_pressed.connect(sent.append)
        terminal.send_text("conf t\n")
        self.assertEqual("".join(sent), "conf t\r")     # 機器相手は素のまま
        del sent[:]
        w.append_output("dev", "\x1b[?2004h")           # bash が立てる
        terminal.send_text("echo hi\n")
        self.assertEqual("".join(sent), "\x1b[200~echo hi\r\x1b[201~")


class DeviceQueryTest(WidgetRenderTest):
    def test_a_cursor_position_query_is_answered(self):
        w = self.widget()
        sent = []
        w._terminals["dev"].key_pressed.connect(sent.append)
        w.append_output("dev", "ab\x1b[6n")
        self.assertEqual(sent, ["\x1b[1;3R"])


if __name__ == "__main__":
    unittest.main()
