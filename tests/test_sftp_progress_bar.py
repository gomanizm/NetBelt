"""SFTP パネルの進捗バーが、いま見ている機器の話だけを映すことを検証する。

set_sftp_manager は _detach_manager() で旧マネージャの transfer_progress /
transfer_complete を切り離し、_current_entries や案内の表示も初期化するが、
進捗バーの値・可視状態・書式だけは触っていなかった。

そのため機器 A への転送中に別タブへ切り替えると、接続先の表示は B に
変わるのに、その直下の進捗バーは A の途中経過（45% (45.0 MB / 100.0 MB)
など）を出したまま残る。A の転送が終わっても _on_transfer_complete は
もう繋がっていないので setVisible(False) されず、バーは消えない。

機器名を出して誤送信を防ぐという接続先表示の意図と正面から食い違い、
「B への転送が 45% で止まっている」と読める表示になる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpProgressBarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        m = mock.Mock()
        m.current_path = "/"
        m.get_current_path.return_value = "/"
        return m

    def _panel_transferring(self):
        """rtrA へ転送中で、進捗バーが出ている状態を作る。"""
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        panel.set_sftp_manager(self._manager(), "rtrA")
        panel._update_progress(45 * 1024 * 1024, 100 * 1024 * 1024)
        self.assertFalse(panel.progress_bar.isHidden(),
                         "前提: 転送中は進捗バーが出ている")
        return panel

    def test_switching_device_hides_the_previous_progress(self):
        """別の機器へ切り替えたら、前の機器の進捗を出したままにしないこと。"""
        panel = self._panel_transferring()
        panel.set_sftp_manager(self._manager(), "rtrB")
        self.assertTrue(panel.progress_bar.isHidden(),
                        "前の機器の進捗バーが残っている")

    def test_switching_device_clears_the_previous_percentage(self):
        """前の機器の割合が残らないこと。

        「B への転送が 45% で止まっている」と読める表示になる。
        """
        panel = self._panel_transferring()
        before = panel.progress_bar.value()
        panel.set_sftp_manager(self._manager(), "rtrB")

        self.assertNotEqual(panel.progress_bar.value(), before,
                            "前の機器の進捗の値が残っている")
        self.assertNotIn("45", panel.progress_bar.format(),
                         "前の機器の割合が書式に残っている: %r"
                         % panel.progress_bar.format())
        self.assertNotIn("45", panel.progress_bar.text(),
                         "前の機器の割合が表示に残っている: %r"
                         % panel.progress_bar.text())

    def test_disconnecting_hides_the_progress_too(self):
        """切断でも消えること（これまでどおり）。"""
        panel = self._panel_transferring()
        panel.clear()
        self.assertTrue(panel.progress_bar.isHidden())

    def test_a_transfer_on_the_new_device_still_shows(self):
        """切り替えた先で転送を始めれば、ちゃんと出ること。"""
        panel = self._panel_transferring()
        panel.set_sftp_manager(self._manager(), "rtrB")
        panel._update_progress(10, 100)
        self.assertFalse(panel.progress_bar.isHidden(),
                         "切り替え後の転送で進捗バーが出ない")
        self.assertIn("10%", panel.progress_bar.format())


if __name__ == "__main__":
    unittest.main()
