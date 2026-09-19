"""上限の手前で窓を縮めて先頭の行が捨てられたら、全ログ保存が欠落を知らせることを検証する。

「全ログ保存」の欠落警告は terminal._log_truncated を見る。この印は、描き
終えたあとの blockCount が上限以上のとき（と、描画が例外で抜けたとき）に
しか立たなかった。ところが先頭の切り捨て量は「押し出し行を書いた時点の
行数」から決めるので、文書が上限の手前にあるときに窓を縦に縮めると、
先頭の行を実際に捨てたのに、最後の blockCount は上限を下回り、印が立たない。

実測（検証役）: 上限 300、24 行の画面で文書 299 ブロック（カーソルは最下行）
から 22 行へ縮めると、ブロック数 299 -> 298 で先頭の L00000 が消えたが、
_log_truncated は False のまま、全ログ保存の警告は 0 回だった。本番の上限
20000 のまま、窓を実際に 2 行縮めても同じだった（19999 -> 19998）。

先頭の行を実際に捨てた場所（_trim_document）で印を立てるようにした。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

LIMIT = 300


class TrimMarksTruncationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.target = os.path.join(
            tempfile.mkdtemp(prefix="netbelt-trimflag-"), "saved.log")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        """上限 LIMIT、24 行 80 桁の画面を持つ端末を返す。"""
        from ui.terminal_widget import TerminalWidget
        mock.patch.object(TerminalWidget, "MAX_DOCUMENT_BLOCKS", LIMIT).start()
        w = TerminalWidget()
        self.addCleanup(w.close)
        self.grid = mock.patch.object(w, "_grid_size", return_value=(24, 80))
        self.grid.start()
        terminal = w.create_terminal_tab("dev")
        w.tab_widget.setCurrentIndex(w.tab_widget.indexOf(terminal))
        return w, terminal

    def _shrink_to_22_rows(self, w):
        """窓を縦に 2 行縮めたのと同じ手順（_apply_grid_size）で縮める。"""
        self.grid.stop()
        mock.patch.object(w, "_grid_size", return_value=(22, 80)).start()
        w._apply_grid_size("dev")

    def test_shrinking_just_below_the_cap_marks_the_dropped_lines(self):
        """上限の手前で縮めて先頭が消えたら、欠落の印が立ち、保存前に知らせること。"""
        from PyQt6.QtWidgets import QMessageBox
        w, terminal = self._widget()
        w.append_output("dev", "".join("L%05d\r\n" % i for i in range(298)))
        document = terminal.document()
        self.assertEqual(document.blockCount(), LIMIT - 1,
                         "前提: 文書が上限の 1 行手前にある")
        self.assertIn("L00000", terminal.toPlainText(), "前提: まだ何も捨てていない")
        self.assertFalse(terminal._log_truncated)

        self._shrink_to_22_rows(w)

        self.assertNotIn("L00000", terminal.toPlainText(),
                         "前提: 縮めたことで先頭の行が捨てられた")
        self.assertLess(document.blockCount(), LIMIT,
                        "前提: 最後の行数は上限を下回っている")
        self.assertTrue(terminal._log_truncated,
                        "先頭の行を捨てたのに、欠落の印が立っていない")

        self.warning.return_value = QMessageBox.StandardButton.Cancel
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")), \
                mock.patch(
                    "ui.dialogs.log_save_dialog.LogSaveProgressDialog") as dlg:
            w.save_current_log()
        self.assertEqual(self.warning.call_count, 1,
                         "欠けたログを、知らせずに保存しようとしている")
        self.assertEqual(dlg.call_count, 0, "取り消したのに保存している")

    def test_nothing_is_marked_while_no_line_is_dropped(self):
        """何も捨てていないうちは、印を立てないこと（黙って保存できる）。"""
        w, terminal = self._widget()
        w.append_output("dev", "".join("L%05d\r\n" % i for i in range(100)))

        self._shrink_to_22_rows(w)

        self.assertIn("L00000", terminal.toPlainText(), "前提: 何も捨てていない")
        self.assertFalse(terminal._log_truncated,
                         "何も捨てていないのに欠落の印が立った")


if __name__ == "__main__":
    unittest.main()
