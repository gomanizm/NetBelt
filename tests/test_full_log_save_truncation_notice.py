"""表示の上限で切れたログを「全ログ保存」するとき、先に断ることを検証する。

10ea883 が表示文書へ MAX_DOCUMENT_BLOCKS 行の上限を入れたため、古い行は
Qt が先頭から捨てる。save_current_log は toPlainText() を保存するので、
その上限は保存物の上限でもある。実測: 30,000 行を流した後の保存物は
20,000 行で、先頭の 'line 000000' は入っていなかった（約 10,000 行が
保存不能）。上限が入る前は全部保存できていたので、show tech-support の
ような数万行の採取を「全ログ保存」で拾う使い方は黙って壊れる。

保存の前に、残っているのが直近何行かと、全量が要るならログ記録を使う
ことを知らせ、そのまま保存するか取り消すかを選ばせる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class FullLogSaveTruncationNoticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-savetrunc-")
        old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, old)
        self.target = os.path.join(self.dir, "saved.log")
        self.information = mock.patch(
            "PyQt6.QtWidgets.QMessageBox.information").start()
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self, cap, lines):
        """文書の上限を cap 行にした端末へ lines 行流して返す。"""
        from ui.terminal_widget import TerminalWidget
        patcher = mock.patch.object(TerminalWidget, "MAX_DOCUMENT_BLOCKS", cap)
        patcher.start()
        self.addCleanup(patcher.stop)
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("dev")
        w.tab_widget.setCurrentIndex(w.tab_widget.indexOf(w._terminals["dev"]))
        for i in range(lines):
            w.append_output("dev", "line %06d\r\n" % i)
        return w

    def test_a_capped_document_is_not_saved_silently(self):
        """上限に達していたら、黙って保存せず先に知らせること。"""
        from PyQt6.QtWidgets import QMessageBox
        w = self._widget(cap=50, lines=200)
        self.assertEqual(w.get_current_terminal().document().blockCount(), 50,
                         "前提: 文書が上限まで切り詰められている")
        self.warning.return_value = QMessageBox.StandardButton.Cancel

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")) as chooser, \
                mock.patch(
                    "ui.dialogs.log_save_dialog.LogSaveProgressDialog") as dlg:
            w.save_current_log()

        self.assertEqual(self.warning.call_count, 1,
                         "切り詰められていることを利用者に知らせていない")
        message = self.warning.call_args[0][2]
        self.assertIn("50", message, "残っている行数を示していない")
        self.assertIn("ログ記録", message, "全量を採る手段を案内していない")
        self.assertEqual(chooser.call_count, 0, "取り消したのに保存先を聞いている")
        self.assertEqual(dlg.call_count, 0, "取り消したのに保存している")

    def test_accepting_the_notice_saves_what_is_left(self):
        """知らせを了解したら、残っている分を保存すること。"""
        from PyQt6.QtWidgets import QMessageBox
        w = self._widget(cap=50, lines=200)
        self.warning.return_value = QMessageBox.StandardButton.Ok

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")), \
                mock.patch(
                    "ui.dialogs.log_save_dialog.LogSaveProgressDialog") as dlg:
            w.save_current_log()

        self.assertEqual(dlg.call_count, 1, "了解したのに保存していない")
        saved = dlg.call_args[0][0]
        self.assertIn("line 000199", saved, "直近の行が保存されていない")
        self.assertNotIn("line 000000", saved,
                         "前提: 古い行は表示から消えている")

    def test_a_shrunken_window_does_not_silence_the_notice(self):
        """一度切り詰めたら、窓を縮めて行数が減っても知らせ続けること。

        判定が現在の blockCount だけだったため、上限に達して古い行が実際に
        捨てられた後でも、窓を 1 行ぶん縦に縮めるだけで blockCount が上限を
        下回り、以後は知らせが出なくなった。欠けたログが「ログ保存完了」と
        して黙って書き出される。
        """
        from PyQt6.QtWidgets import QMessageBox
        w = self._widget(cap=50, lines=200)
        terminal = w.get_current_terminal()
        self.assertEqual(terminal.document().blockCount(), 50,
                         "前提: 文書が上限まで切り詰められている")
        self.assertNotIn("line 000000", terminal.toPlainText(),
                         "前提: 古い行は実際に捨てられている")

        # 窓を 1 行ぶん縦に縮める（_apply_grid_size と同じ手順）
        screen = terminal._screen
        screen.set_size(screen.rows - 1, screen.cols)
        w._render_screen(terminal)
        self.assertLess(terminal.document().blockCount(), 50,
                        "前提: 縮めたことで blockCount が上限を下回った")

        self.warning.return_value = QMessageBox.StandardButton.Cancel
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")), \
                mock.patch(
                    "ui.dialogs.log_save_dialog.LogSaveProgressDialog") as dlg:
            w.save_current_log()

        self.assertEqual(self.warning.call_count, 1,
                         "切り詰め済みなのに知らせずに保存しようとしている")
        self.assertEqual(dlg.call_count, 0, "取り消したのに保存している")

    def test_a_log_within_the_cap_is_saved_without_a_notice(self):
        """上限に達していなければ、これまでどおり黙って保存すること。"""
        w = self._widget(cap=50, lines=5)

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.target, "")), \
                mock.patch(
                    "ui.dialogs.log_save_dialog.LogSaveProgressDialog") as dlg:
            w.save_current_log()

        self.assertEqual(self.warning.call_count, 0,
                         "切れていないのに知らせが出ている")
        self.assertEqual(dlg.call_count, 1, "保存が始まっていない")


if __name__ == "__main__":
    unittest.main()
