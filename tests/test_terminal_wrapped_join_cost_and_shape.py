"""画面の折り返し行を繋ぐ処理（再接続・コピー・全ログ保存）の重さと形を検証する。

何が起きていたか（実測、基準 097550c）:
1. 再接続が画面の大きさの 2 乗で固まっていた。再接続（_attach_screen）は、
   出ていく画面の折り返し行を _join_wrapped_blocks で 1 ブロックへ繋ぐ。その
   区切り文字を 1 つずつ別々の編集で消していたので、消すたびに伸びていく
   ブロックを Qt が組み直し、変更通知（contentsChanged）も折り返し 1 行ごとに
   出ていた（10 行が全部折り返しの画面で 9 回）。改行の無い出力（base64 の塊・
   バイナリの cat・長い 1 行）で画面を埋めて再接続すると、100x300 で 0.90 秒、
   125x475 で 4.19 秒、200x500 で 18.6 秒 GUI が止まった（繋ぐ処理を足す前の
   aa38a2b では、検査役の実測でどれも 0.001 秒）。
2. 右端の桁が空白の折り返し行は、繋ぐと空白が消えていた。画面領域はすべての
   行で末尾の既定属性の空白を刈って文書へ書く（_visible_cells）。20 桁の画面に
   'ip route 192.0.2.64 255.255.255.192 198.51.100.1'（20 桁目が空白）を出すと、
   コピーは 'ip route 192.0.2.64255.255.255.192 198.51.100.1' になり、再接続で
   この形が文書に確定して全ログ保存も同じになった。押し出された履歴だけは
   折り返し行を刈らずに書くので正しかった。
3. 画面に段落の区切りになる文字（U+2029 / U+FDD0 / U+FDD1）があると、繋ぐ
   ブロックを取り違えていた。_wrapped_blocks は「画面の r 行目 = 画面領域の先頭
   ブロックから r 個あと」としていたが、その文字は文書の上でブロックを 1 つ
   増やす。'A<U+2029>B' / 'line1' / 折り返す 26 文字、の画面では、コピーが
   'line1' と次の行を繋ぎ、本物の折り返しは割れたままだった。折り返し行の中に
   その文字があると、機器が送った区切りの方を繋いでいた。再接続では、この
   取り違えのまま区切り文字を消すので、本物の改行が文書から消えた。

どう直したか:
1. 区切り文字を消す操作を、1 本のカーソルの beginEditBlock/endEditBlock で
   1 回の編集にまとめた（変更通知は 1 回になる）。
2. 画面領域でも、折り返しで次の行へ続く行（最後の行を除く。_wrapped_blocks と
   同じ範囲）は末尾を刈らずに書く。押し出された履歴と同じ扱い。全角のために
   右端で空けたセルは Screen 側が行から消しているので、余分な空白は入らない。
3. _wrapped_blocks で、その行までに含まれる区切り文字の数だけブロック番号を
   後ろへずらす。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

LONG = "0123456789ABCDEFGHIJklmnop"
# 20 桁の画面で、20 桁目（右端）がちょうど空白になる設定行
ROUTE = "ip route 192.0.2.64 255.255.255.192 198.51.100.1"
# QTextCursor.insertText が段落の区切りに変える文字のうち、パーサがセルへ入れるもの
SEPARATORS = (chr(0x2029), chr(0xFDD0), chr(0xFDD1))


class WrappedJoinCostAndShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtWidgets import QApplication, QMessageBox
        QApplication.clipboard().setText("BEFORE")
        for name in ("warning", "information", "critical"):
            patcher = mock.patch.object(
                QMessageBox, name,
                return_value=QMessageBox.StandardButton.Ok)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _terminal(self, rows=5, cols=20):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        self.addCleanup(w._pending_output.clear)
        self.addCleanup(w._output_timer.stop)
        w.resize(900, 500)
        w.show()
        terminal = w.create_terminal_tab("dev")
        self.app.processEvents()
        terminal._screen.set_size(rows, cols)
        w._render_screen(terminal)
        return w, terminal

    def _reconnect(self, w, rows=5, cols=20):
        """同じ機器名で create_terminal_tab を呼び直す（再接続の経路）。"""
        terminal = w.create_terminal_tab("dev")
        self.app.processEvents()
        terminal._screen.set_size(rows, cols)
        return terminal

    def _copy_all(self, terminal):
        from PyQt6.QtGui import QTextCursor
        from PyQt6.QtWidgets import QApplication
        cursor = terminal.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        terminal.setTextCursor(cursor)
        terminal.copy()
        return QApplication.clipboard().text()

    def _save_all(self, w):
        from PyQt6.QtWidgets import QFileDialog
        path = os.path.join(tempfile.mkdtemp(prefix="termui03j-"), "all.log")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(path, "")):
            w.save_current_log()
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _fill_without_breaks(self, w, terminal):
        """改行の無い出力で画面を埋める（最後の 1 文字は残して押し出さない）。"""
        screen = terminal._screen
        w.append_output("dev", "x" * (screen.rows * screen.cols - 1))
        self.app.processEvents()
        self.assertEqual(sum(screen.wrapped), screen.rows - 1,
                         "前提: 最後の行を除く全行が折り返しになっていない")

    # --- 1. 再接続の重さ ---

    def test_joining_a_screen_of_wrapped_rows_is_a_single_edit(self):
        """全行が折り返しの画面で再接続しても、文書の変更通知が 1 回で済むこと。"""
        w, terminal = self._terminal(10, 20)
        self._fill_without_breaks(w, terminal)
        document = terminal.document()
        seen = []
        slot = lambda: seen.append(1)   # noqa: E731
        document.contentsChanged.connect(slot)
        try:
            w.create_terminal_tab("dev")
        finally:
            document.contentsChanged.disconnect(slot)
        self.assertIn("x" * 199, terminal.toPlainText(),
                      "前提: 折り返し行が 1 行に繋がっていない")
        self.assertLessEqual(
            len(seen), 1,
            "折り返し行を繋ぐのに文書の編集が %d 回に分かれている"
            "（1 回ごとに伸びていくブロックを組み直す）" % len(seen))

    def test_reconnecting_a_large_screen_of_wrapped_rows_does_not_freeze(self):
        """200x500 の画面が全部折り返しでも、再接続が 2 秒かからないこと。

        直す前は 18.6 秒（125x475 で 4.19 秒）。直した後は 0.1 秒前後。
        """
        w, terminal = self._terminal(200, 500)
        self._fill_without_breaks(w, terminal)
        started = time.perf_counter()
        w.create_terminal_tab("dev")
        took = time.perf_counter() - started
        self.assertIn("x" * (200 * 500 - 1), terminal.toPlainText(),
                      "前提: 折り返し行が 1 行に繋がっていない")
        self.assertLess(took, 2.0,
                        "200x500 の再接続に %.2f 秒かかった" % took)

    # --- 2. 右端の空白 ---

    def test_a_blank_at_the_wrap_column_is_kept_on_the_screen(self):
        """画面のままコピーしても、折り返し位置の空白が消えないこと。"""
        w, terminal = self._terminal()
        w.append_output("dev", ROUTE + "\r\n")
        self.app.processEvents()
        self.assertIn(ROUTE, self._copy_all(terminal),
                      "右端の空白が消えて、前後の語がくっついた")

    def test_a_blank_at_the_wrap_column_survives_a_reconnect(self):
        """再接続の後のコピーと全ログ保存でも、折り返し位置の空白が残ること。"""
        w, terminal = self._terminal()
        w.append_output("dev", ROUTE + "\r\n")
        self.app.processEvents()

        self._reconnect(w)

        self.assertIn(ROUTE, self._copy_all(terminal),
                      "再接続の後のコピーで、右端の空白が消えている")
        self.assertIn(ROUTE, self._save_all(w),
                      "再接続の後の全ログ保存で、右端の空白が消えている")

    def test_a_blank_at_the_wrap_column_in_pushed_out_history(self):
        """（対照）押し出された履歴では、元から空白が残ること。"""
        w, terminal = self._terminal()
        w.append_output("dev", ROUTE + "\r\n" + "x\r\n" * 8)
        self.app.processEvents()
        self.assertIn(ROUTE, self._copy_all(terminal))

    def test_a_short_unwrapped_row_still_drops_trailing_blanks(self):
        """（対照）折り返していない行は、これまでどおり末尾の空白を刈ること。"""
        w, terminal = self._terminal()
        w.append_output("dev", "abc   \r\nnext\r\n")
        self.app.processEvents()
        self.assertIn("abc\nnext", self._copy_all(terminal))

    # --- 3. 段落の区切りになる文字 ---

    def _show_each_separator(self, text_for):
        """区切り文字ごとに、text_for(sep) を出した端末を作って (sep, w, terminal) を返す。"""
        for sep in SEPARATORS:
            w, terminal = self._terminal()
            w.append_output("dev", text_for(sep))
            self.app.processEvents()
            yield "U+%04X" % ord(sep), w, terminal

    @staticmethod
    def _above(sep):
        return "A" + sep + "B\r\nline1\r\n" + LONG + "\r\n"

    @staticmethod
    def _inside(sep):
        # 20 桁の 1 行目の途中に区切り、右端で折り返して TAIL が 2 行目へ
        return "top\r\n01234" + sep + "abcdefghijklmn" + "TAIL\r\n"

    ABOVE = "line1\n" + LONG
    INSIDE = "01234\nabcdefghijklmnTAIL"

    def test_a_separator_above_does_not_shift_the_join_on_the_screen(self):
        """上の行に区切り文字があっても、画面のままのコピーは本物の折り返しを繋ぐこと。"""
        for name, w, terminal in self._show_each_separator(self._above):
            with self.subTest(sep=name):
                self.assertIn(self.ABOVE, self._copy_all(terminal),
                              "繋ぐ行を取り違えた")

    def test_a_separator_above_keeps_the_real_break_after_a_reconnect(self):
        """上の行に区切り文字があっても、再接続で本物の改行を消さないこと。"""
        for name, w, terminal in self._show_each_separator(self._above):
            with self.subTest(sep=name):
                self._reconnect(w)
                self.assertIn(self.ABOVE, self._copy_all(terminal),
                              "再接続の後のコピーで、繋ぐ行を取り違えた")
                self.assertIn(self.ABOVE, self._save_all(w),
                              "再接続の後の全ログ保存で、繋ぐ行を取り違えた")

    def test_a_separator_inside_a_wrapped_row_stays_on_the_screen(self):
        """折り返し行の中の区切り文字は区切りのまま、折り返しの方が繋がること。

        押し出された履歴と同じ形（区切りはコピーで改行になる）。
        """
        for name, w, terminal in self._show_each_separator(self._inside):
            with self.subTest(sep=name):
                self.assertIn(self.INSIDE, self._copy_all(terminal),
                              "区切りと折り返しを取り違えた")

    def test_a_separator_inside_a_wrapped_row_survives_a_reconnect(self):
        """再接続の後も、機器が送った区切りが文書に残ること。"""
        for name, w, terminal in self._show_each_separator(self._inside):
            with self.subTest(sep=name):
                self._reconnect(w)
                self.assertIn(self.INSIDE, self._copy_all(terminal),
                              "再接続で、区切りが消えて折り返しが割れたまま")

    def test_a_separator_inside_a_wrapped_row_in_pushed_out_history(self):
        """（対照）押し出された履歴では、元から区切りが残り折り返しが繋がること。"""
        for name, w, terminal in self._show_each_separator(
                lambda sep: self._inside(sep) + "x\r\n" * 8):
            with self.subTest(sep=name):
                self.assertIn(self.INSIDE, self._copy_all(terminal))


if __name__ == "__main__":
    unittest.main()
