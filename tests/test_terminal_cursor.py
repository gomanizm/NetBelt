"""ターミナルの行編集表示（カーソル移動時に既存文字が消えない）を検証する。

制御コードの扱いは IOSv 実機(192.0.2.210)で採取したバイト列に基づく:
  左矢印        -> \x08 * n            （\b はカーソル左移動であって削除ではない）
  途中に X 入力 -> "X running-config" + \x08 * 15   （残りを再描画してから戻る）
  BackSpace     -> \x08 + " running-config " + \x08 * 16
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

BS = "\x08"
ESC = "\x1b"


class TerminalCursorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        return w

    def _text(self, w):
        return w._terminals["dev"].toPlainText().replace("\n", "")

    def test_backspace_moves_cursor_without_deleting(self):
        # 本件の不具合: 左に戻ると後続テキストが画面から消えていた
        w = self._terminal()
        w.append_output("dev", "shoe running-config")
        w.append_output("dev", BS * 15)
        self.assertIn("running-config", self._text(w),
                      "カーソルを戻しただけで後続テキストが消えた")

    def test_esc_left_keeps_following_text(self):
        # ESC[D 形式で左移動する機器でも同様に消えない
        w = self._terminal()
        w.append_output("dev", "shoe running-config")
        w.append_output("dev", (ESC + "[D") * 15)
        self.assertIn("running-config", self._text(w))

    def test_real_device_sequence_left_then_insert(self):
        # 実測どおりに再生: 左移動 -> "X" + 残りの再描画 -> 戻る
        w = self._terminal()
        w.append_output("dev", "shoe running-config")
        w.append_output("dev", BS * 15)
        w.append_output("dev", "X running-config" + BS * 15)
        self.assertIn("shoeX running-config", self._text(w))

    def test_real_device_backspace_sequence(self):
        # 実測どおりの削除列で 'e' が 1 文字だけ消える
        w = self._terminal()
        w.append_output("dev", "shoe running-config")
        w.append_output("dev", BS * 15)
        w.append_output("dev", BS + " running-config " + BS * 16)
        text = self._text(w)
        # 実測列を手で追った正解: "sho running-config " （末尾に上書き用の空白）
        self.assertIn("sho running-config", text)
        self.assertNotIn("shoe", text)

    def test_overwrite_not_insert_in_middle(self):
        # 端末は上書き描画。行の途中に書くと既存文字を置き換える
        w = self._terminal()
        w.append_output("dev", "abcdef")
        w.append_output("dev", (ESC + "[D") * 3 + "Zdef")
        self.assertIn("abcZdef", self._text(w))

    def test_erase_to_end_of_line_then_redraw(self):
        # ESC[K で行末まで消したあと、送り直された文字列が表示される
        w = self._terminal()
        w.append_output("dev", "show ip int brief")
        w.append_output("dev", (ESC + "[D") * 9)
        w.append_output("dev", ESC + "[K" + "route")
        text = self._text(w)
        self.assertTrue(text.endswith("route"), "再描画された文字列が出ていない: %r" % text)
        self.assertNotIn("brief", text)

    def test_newline_still_appends(self):
        # 通常の出力（改行を含む複数行）が壊れないこと
        w = self._terminal()
        w.append_output("dev", "line1\r\nline2\r\n")
        text = w._terminals["dev"].toPlainText()
        self.assertIn("line1", text)
        self.assertIn("line2", text)

    def test_nxos_backspace_sequence(self):
        """NXOSv 実測(192.0.2.240)の削除列を再生する。

          BackSpace ->  + "nfig" + ESC[J + ESC[4D
          （左へ1つ戻り、残りを再描画し、ESC[J で以降を消してから戻る）
        期待: "shoe running-cnfig"（カーソル位置の o が消える）
        """
        w = self._terminal()
        w.append_output("dev", "shoe running-config")
        w.append_output("dev", (ESC + "[D") * 4)          # 末尾から4つ戻る
        w.append_output("dev", BS + "nfig" + ESC + "[J" + ESC + "[4D")
        text = self._text(w)
        # 手計算の正解: co|nfig の o が消えて "shoe running-cnfig"
        self.assertEqual(text, "shoe running-cnfig")
        self.assertFalse(text.endswith("gg"), "ESC[J が効かず余分な文字が残っている: %r" % text)

    def test_esc_j_clears_to_end(self):
        # ESC[J（既定=0）はカーソル以降を消す
        w = self._terminal()
        w.append_output("dev", "abcdef")
        w.append_output("dev", (ESC + "[D") * 3 + ESC + "[J")
        self.assertEqual(self._text(w), "abc")

    def test_esc_k_variants(self):
        # ESC[1K は行頭からカーソルまで (カーソル位置を含む) を空白に
        # する。ECMA-48 の EL は消した分を詰めない。旧実装は削除して
        # 左へ詰めており ("def")、桁がずれる方が誤りだった
        w = self._terminal()
        w.append_output("dev", "abcdef")
        w.append_output("dev", (ESC + "[D") * 3 + ESC + "[1K")
        self.assertEqual(self._text(w), "    ef")

if __name__ == "__main__":
    unittest.main()
