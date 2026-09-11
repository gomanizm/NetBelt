"""BMP 外の文字が出ても、描画位置がずれないことを検証する。

_render_screen の差分更新は、文書から取り出した文字列と新しい文字列を
Python の文字単位で比べ、その添字をそのまま QTextCursor.setPosition へ
渡していた。QTextDocument の位置は UTF-16 のコード単位で数えるのに対し、
Python の len() はコードポイント数なので、BMP 外の文字（絵文字、CJK
拡張B など）が1つ画面に出た時点で1つずつずれる。

ずれると、書き換え範囲が本来より手前を指す。改行（ブロック区切り）の
手前へ文字を挿し込んだり、隣の文字を巻き込んで消したりして、以後直らない。
同じずれは行ごとの塗り直しの位置計算と、キャレット位置にも波及する。

画面モデル側は正しいので、壊れるのは表示と、それをそのまま読む
「全ログ保存」の内容である。Linux の MOTD に絵文字が入っている、
機器が UTF-8 で装飾を出す、といった場面で踏む。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

EMOJI = "\U0001F600"        # 😀 BMP 外（UTF-16 では2コード単位）


class TerminalNonBmpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, rows=10, cols=40):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w._grid_size = lambda t, r=rows, c=cols: (r, c)
        w.create_terminal_tab("dev")
        w._apply_grid_size("dev")
        return w, w._terminals["dev"]

    @staticmethod
    def _model_lines(terminal):
        return [line.rstrip() for line in terminal._screen.text()]

    @staticmethod
    def _document_lines(terminal, count):
        text = terminal.toPlainText()
        return [line.rstrip() for line in text.split("\n")[-count:]]

    def _assert_document_matches_model(self, terminal, note=""):
        model = self._model_lines(terminal)
        shown = self._document_lines(terminal, len(model))
        self.assertEqual(shown, model,
                         "表示が画面モデルと食い違っている %s" % note)

    def test_output_after_a_non_bmp_character_is_not_shifted(self):
        """BMP 外の文字のあとに来た出力が、そのまま読めること。"""
        w, terminal = self._widget()
        w.append_output("dev", EMOJI + " hello\r\n")
        w.append_output("dev", "second line\r\n")

        text = terminal.toPlainText()
        self.assertIn("second line", text, "後続の行が壊れている")
        self.assertIn(EMOJI + " hello", text, "BMP 外を含む行が壊れている")

    def test_the_document_matches_the_model_after_a_non_bmp_character(self):
        """画面モデルと表示が一致すること（差分更新を何度も通す）。"""
        w, terminal = self._widget()
        w.append_output("dev", EMOJI + " start\r\n")
        for i in range(5):
            w.append_output("dev", "row %d\r\n" % i)
            self._assert_document_matches_model(terminal, "(%d 行目のあと)" % i)

    def test_rewriting_a_line_after_a_non_bmp_character_is_exact(self):
        """カーソルを戻して書き直しても、隣の文字を巻き込まないこと。"""
        w, terminal = self._widget()
        w.append_output("dev", EMOJI + "ABCDEF\r\n")
        # 1行目の先頭へ戻って書き直す（差分更新の経路）
        w.append_output("dev", "\x1b[1;1H" + EMOJI + "ABCXEF")

        self._assert_document_matches_model(terminal)

    def test_several_non_bmp_characters_do_not_accumulate_drift(self):
        """複数あってもずれが積み上がらないこと。"""
        w, terminal = self._widget()
        w.append_output("dev", (EMOJI * 4) + " tail\r\n")
        w.append_output("dev", "after\r\n")

        self._assert_document_matches_model(terminal)

    def test_the_caret_lands_on_the_screen_cursor(self):
        """キャレットが画面カーソルの位置に来ること。

        ずれていると、入力位置が実際の桁と食い違って見える。
        """
        w, terminal = self._widget()
        w.append_output("dev", EMOJI + "abc")

        screen = terminal._screen
        # 絵文字は 2 セル占めるので、桁で文字列を切らずセルで切る
        cells = screen.lines[screen.cursor_row][:screen.cursor_col]
        head = "".join(cell[0] for cell in cells)
        # 段落内の位置で見る（文書全体の絶対位置を組み直さずに済む）。
        # Qt が数える単位に合わせて UTF-16 のコード単位で期待値を出す。
        expected = len(head.encode("utf-16-le")) // 2

        self.assertEqual(terminal.textCursor().positionInBlock(), expected,
                         "キャレットが画面カーソルとずれている"
                         "（絵文字ぶんだけ手前を指している）")

    # --- BMP だけのときの動きは変えないこと ---

    def test_plain_text_still_matches_the_model(self):
        w, terminal = self._widget()
        w.append_output("dev", "Router#show version\r\n")
        w.append_output("dev", "IOS-XE\r\n")
        self._assert_document_matches_model(terminal)


if __name__ == "__main__":
    unittest.main()
