"""出力への追従と、溜めて描く出力の順序・固まり方を検証する（検査役の指摘）。

B1: 最下部を見ているかを描画のたびに「スクロールバーの値 < 最大値」で決めて
    いたので、窓を縦に縮めたり文字を大きくしたりすると、利用者が何も動かして
    いないのに値が最大値より小さくなり、以後の出力に追従しなくなった（実測:
    高さを 1px 縮めると value=4059 max=4060、40 行届いても最後の行が見えない）。
B4: 受信を溜めている最中に切断などの案内（show_notice）が出ると、溜まった分を
    その場で全部描くので固まった（実測: 0.92MB 溜まりで 2.51 秒、2.92MB で 7.27 秒）。
B2: 溜めた出力を 1 片描いている最中に割り込んだ出力や案内が、描き残しより先に
    出て、画面の順序が入れ替わった（実測: [A 322 行, LATE, A 1178 行]）。

あわせて、テストが守っていなかった部品（貼り付け・IME 確定で最下部へ戻る、
閉じたタブの溜まり分を捨てる、再接続の前に前の画面へ描き切る）を固定する。
"""
import os
import re
import sys
import time
import unittest

sys.path.insert(0, "src")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, width=900, height=500):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.resize(width, height)
        w.show()
        terminal = w.create_terminal_tab("dev")
        self.app.processEvents()
        return w, terminal

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _drain(self, w, seconds=60):
        end = time.time() + seconds
        while time.time() < end and (w._pending_output or w._output_timer.isActive()):
            self.app.processEvents()
        self.app.processEvents()

    @staticmethod
    def _last_line_visible(terminal, needle):
        from PyQt6.QtCore import QPoint
        viewport = terminal.viewport()
        bottom = terminal.cursorForPosition(QPoint(0, viewport.height() - 2))
        block = bottom.block()
        for _ in range(60):
            if needle in block.text():
                return True
            block = block.previous()
            if not block.isValid():
                break
        return False


class FollowingSurvivesLayoutChangesTest(_Base):
    def _feed(self, w, start, count):
        w.append_output("dev", "".join("line %06d\r\n" % i
                                       for i in range(start, start + count)))
        self.app.processEvents()

    def test_following_survives_a_shorter_window(self):
        """最下部で見ているときに窓を縦に縮めても、出力に追従し続けること。"""
        w, terminal = self._widget()
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum(), "前提: 最下部を見ている")

        w.resize(900, 480)
        self._pump(0.4)          # 格子の再計算は 200ms まとめてから
        self._feed(w, 300, 40)

        self.assertEqual(bar.value(), bar.maximum(), "縮めたあと追従しなくなった")
        self.assertTrue(self._last_line_visible(terminal, "line 000339"),
                        "最後の行が見えていない")

    def test_following_survives_a_bigger_font(self):
        """文字を大きくしても、出力に追従し続けること。"""
        w, terminal = self._widget()
        w.apply_terminal_settings({"font_size": 10, "font_family": "Consolas",
                                   "background_color": "#000000",
                                   "text_color": "#FFFFFF"})
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum(), "前提: 最下部を見ている")

        w.apply_terminal_settings({"font_size": 12, "font_family": "Consolas",
                                   "background_color": "#000000",
                                   "text_color": "#FFFFFF"})
        self._pump(0.4)
        self._feed(w, 300, 40)

        self.assertEqual(bar.value(), bar.maximum(), "文字を大きくしたら追従しなくなった")

    def test_scrolling_back_to_the_bottom_follows_again(self):
        """上へスクロールして止まったあと、最下部まで戻せばまた追従すること。"""
        w, terminal = self._widget()
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        bar.setValue(0)
        self._feed(w, 300, 5)
        self.assertEqual(bar.value(), 0, "前提: 上へスクロール中は動かない")

        bar.setValue(bar.maximum())
        self._feed(w, 305, 40)

        self.assertEqual(bar.value(), bar.maximum(), "最下部へ戻したのに追従しない")

    def test_pasting_returns_to_the_bottom(self):
        """貼り付けたら最下部へ戻ること。"""
        w, terminal = self._widget()
        terminal.set_input_enabled(True)
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        bar.setValue(0)

        self.assertTrue(terminal.send_text("show clock"))

        self.assertEqual(bar.value(), bar.maximum(), "貼り付けても最下部へ戻らない")

    def test_committing_ime_input_returns_to_the_bottom(self):
        """日本語入力の確定でも最下部へ戻ること。"""
        from PyQt6.QtGui import QInputMethodEvent
        w, terminal = self._widget()
        terminal.set_input_enabled(True)
        self._feed(w, 0, 300)
        bar = terminal.verticalScrollBar()
        bar.setValue(0)

        event = QInputMethodEvent("", [])
        event.setCommitString("あ")
        terminal.inputMethodEvent(event)

        self.assertEqual(bar.value(), bar.maximum(), "確定しても最下部へ戻らない")


class QueuedOutputStaysInOrderTest(_Base):
    def _backlog(self, w, lines):
        text = "".join("A%06d %s\r\n" % (i, "x" * 40) for i in range(lines))
        for i in range(0, len(text), 4096):
            w.queue_output("dev", text[i:i + 4096])

    def test_a_notice_during_a_backlog_does_not_draw_it_all_at_once(self):
        """溜まっている最中の案内で、溜まり分を一度に描いて固まらないこと。"""
        w, terminal = self._widget()
        self._backlog(w, 8000)

        t0 = time.perf_counter()
        w.show_notice("dev", "\n接続が切断されました\n")
        took = time.perf_counter() - t0
        self._drain(w)

        self.assertLess(took, 0.2, "案内を出すのに %.2f 秒止まった" % took)
        text = terminal.toPlainText()
        self.assertIn("接続が切断されました", text)
        self.assertLess(text.index("A007999"), text.index("接続が切断されました"),
                        "案内が溜まっていた出力より先に出た")

    def test_output_arriving_while_a_slice_is_drawn_stays_in_order(self):
        """1 片を描いている最中に届いた出力と案内が、描き残しを追い越さないこと。"""
        w, terminal = self._widget()
        self._backlog(w, 1500)
        real = w._render_screen
        fired = []

        def render_and_interrupt(t):
            real(t)
            if not fired:
                fired.append(True)
                # 描いている最中にモーダルの警告などでイベントループが回り、
                # 次の受信と案内が届いた場合を模す
                w.queue_output("dev", "LATE\r\n")
                w.show_notice("dev", "BANNER\n")

        w._render_screen = render_and_interrupt
        self._drain(w)

        text = terminal.toPlainText()
        numbers = [int(n) for n in re.findall(r"A(\d{6})", text)]
        self.assertEqual(numbers, list(range(1500)), "溜まっていた出力が欠けた・入れ替わった")
        self.assertIn("LATE", text)
        self.assertIn("BANNER", text)
        self.assertLess(text.index("A001499"), text.index("LATE"),
                        "描き残しより先に、割り込んだ出力が出た")
        self.assertLess(text.index("LATE"), text.index("BANNER"))

    def test_a_reopened_tab_does_not_show_output_queued_for_the_closed_one(self):
        """閉じたタブに溜まっていた出力が、同じ名前で開き直したタブに出ないこと。"""
        w, terminal = self._widget()
        w.queue_output("dev", "OLD SESSION\r\n")
        index = [w.tab_widget.tabText(i)
                 for i in range(w.tab_widget.count())].index("dev")
        w._close_tab(index)
        reopened = w.create_terminal_tab("dev")
        self._drain(w)

        self.assertNotIn("OLD SESSION", reopened.toPlainText())

    def test_reconnecting_draws_the_old_session_on_the_old_screen(self):
        """再接続で画面を付け直す前に、前のセッションの溜まり分を描き切ること。

        描き切らないと、前のセッションが送った全画面モードの開始などが
        新しいセッションの画面へ持ち越される。
        """
        w, terminal = self._widget()
        w.queue_output("dev", "\x1b[?1049hOLD FULLSCREEN APP\r\n")

        again = w.create_terminal_tab("dev")     # 再接続は同じタブを使い直す
        self.assertIs(again, terminal)
        self._drain(w)

        self.assertFalse(terminal._screen.alt_active,
                         "前のセッションの全画面モードが新しい画面へ持ち越された")


if __name__ == "__main__":
    unittest.main()
