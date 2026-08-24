"""メニューバーのアクションが実際に動くこと。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class MenuActionsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-menu-")
        # 起動時の更新チェックは実際に GitHub API を叩くのでモックする
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(config_path=os.path.join(d, "config.json"))
            return MainWindow()

    def test_copy_and_paste_use_shift_shortcuts(self):
        """Ctrl+C は機器への中断送信に残す。"""
        w = self._window()
        self.assertEqual(w.copy_action.shortcut().toString(), "Ctrl+Shift+C")
        self.assertEqual(w.paste_action.shortcut().toString(), "Ctrl+Shift+V")

    def test_copy_calls_copy_on_the_current_terminal(self):
        w = self._window()
        terminal = w.terminal_widget.get_current_terminal()
        with mock.patch.object(terminal, "copy") as copy_call:
            w._on_copy()
        copy_call.assert_called_once()

    def test_paste_sends_to_the_current_interactive_terminal(self):
        w = self._window()
        terminal = w.terminal_widget.create_terminal_tab("ルータA")
        with mock.patch.object(terminal, "custom_paste") as paste_call:
            w._on_paste()
        paste_call.assert_called_once()

    def test_paste_is_ignored_on_the_home_tab(self):
        """ホームタブは読み取り専用の QTextEdit で custom_paste を持たない。"""
        w = self._window()
        w._on_paste()  # 例外が出ないこと


if __name__ == "__main__":
    unittest.main()
