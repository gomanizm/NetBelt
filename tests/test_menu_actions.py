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
        terminal.set_input_enabled(True)   # 接続成功を模す
        with mock.patch.object(terminal, "custom_paste") as paste_call:
            w._on_paste()
        paste_call.assert_called_once()

    def test_paste_is_ignored_on_a_tab_that_never_connected(self):
        """タブはあるがまだ接続していない状態でも、黙って無視しないこと。"""
        w = self._window()
        w.terminal_widget.create_terminal_tab("未接続")
        messages = []
        w.status_bar.showMessage = lambda text, *a: messages.append(text)
        w._on_paste()
        self.assertEqual(len(messages), 1)

    def test_paste_is_ignored_on_the_home_tab(self):
        """ホームタブは読み取り専用の QTextEdit で custom_paste を持たない。"""
        w = self._window()
        w._on_paste()  # 例外が出ないこと

    def test_font_size_increase_and_decrease(self):
        w = self._window()
        w.config_manager.set_server_settings("terminal", {"font_size": 10})
        w._on_font_size_increase()
        self.assertEqual(
            w.config_manager.get_server_settings("terminal")["font_size"], 11)
        self.assertEqual(
            w.terminal_widget.tab_widget.widget(0).font().pointSize(), 11)
        w._on_font_size_decrease()
        self.assertEqual(
            w.config_manager.get_server_settings("terminal")["font_size"], 10)

    def test_font_size_stops_at_the_limits(self):
        from ui.main_window import MainWindow
        w = self._window()
        # 増減は「いま適用されている値」を基準にするので、config へ書いたあと
        # 適用まで行って初めて「上限にいる」状態になる
        w.config_manager.set_server_settings("terminal", {"font_size": MainWindow.FONT_SIZE_MAX})
        w._apply_terminal_settings_from_config()
        w._on_font_size_increase()
        self.assertEqual(
            w.config_manager.get_server_settings("terminal")["font_size"],
            MainWindow.FONT_SIZE_MAX)
        w.config_manager.set_server_settings("terminal", {"font_size": MainWindow.FONT_SIZE_MIN})
        w._apply_terminal_settings_from_config()
        w._on_font_size_decrease()
        self.assertEqual(
            w.config_manager.get_server_settings("terminal")["font_size"],
            MainWindow.FONT_SIZE_MIN)

    def test_font_size_does_not_wipe_other_settings(self):
        """set_server_settings は浅いマージなので ui_layout が消えないこと。"""
        w = self._window()
        w.config_manager.config.setdefault("settings", {})["ui_layout"] = {"tool_tab": 2}
        w._on_font_size_increase()
        self.assertEqual(
            w.config_manager.config["settings"]["ui_layout"], {"tool_tab": 2})

    def test_apply_terminal_settings_from_config_reads_the_config(self):
        """メソッド単体: config の値をターミナルへ渡すこと。"""
        w = self._window()
        w.config_manager.set_server_settings(
            "terminal", {"font_family": "Courier New", "font_size": 15})
        w._apply_terminal_settings_from_config()
        self.assertEqual(
            w.terminal_widget.tab_widget.widget(0).font().pointSize(), 15)

    def test_terminal_settings_are_applied_when_the_window_opens(self):
        """起動時の配線そのものを見る。

        上のテストはメソッドを直接呼ぶので、__init__ からの呼び出しを
        削除しても落ちない。ユーザーから見える「起動したら設定が効いている」
        という振る舞いは、窓を実際に開いて確かめないと守れない。
        """
        from PyQt6.QtGui import QColor, QPalette
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager

        d = tempfile.mkdtemp(prefix="netbelt-startup-")
        path = os.path.join(d, "config.json")
        # 窓を開く前に config へ書いておく
        ConfigManager(config_path=path).set_server_settings("terminal", {
            "font_family": "Courier New", "font_size": 15,
            "background_color": "#112233", "text_color": "#445566"})

        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(config_path=path)
            w = MainWindow()

        home = w.terminal_widget.tab_widget.widget(0)
        self.assertEqual(home.font().family(), "Courier New")
        self.assertEqual(home.font().pointSize(), 15)
        self.assertEqual(home.palette().color(QPalette.ColorRole.Base),
                         QColor("#112233"))
        self.assertEqual(home.palette().color(QPalette.ColorRole.Text),
                         QColor("#445566"))

    def test_font_size_survives_a_corrupted_config_value(self):
        """メニューは config の生値ではなく実際に適用されている値を基準にすること。

        生値をそのまま加算すると "12" や null で TypeError になり、
        true や 1000 では表示と無関係な値へ飛ぶ。
        """
        from ui.terminal_widget import TerminalWidget
        default_size = TerminalWidget.DEFAULT_TERMINAL_SETTINGS["font_size"]
        for broken in ("12", None, True, 1000):
            with self.subTest(font_size=broken):
                w = self._window()
                w.config_manager.set_server_settings("terminal", {"font_size": broken})
                w._apply_terminal_settings_from_config()
                w._on_font_size_increase()
                self.assertEqual(
                    w.config_manager.get_server_settings("terminal")["font_size"],
                    default_size + 1)
                self.assertEqual(
                    w.terminal_widget.tab_widget.widget(0).font().pointSize(),
                    default_size + 1)

    def test_font_size_reports_a_failed_save(self):
        """保存できなかったことを黙らないこと。"""
        w = self._window()
        w.config_manager.save_config = mock.Mock(return_value=False)
        messages = []
        w.status_bar.showMessage = lambda text, *a: messages.append(text)
        w._on_font_size_increase()
        self.assertEqual(len(messages), 1)
        self.assertIn("保存できませんでした", messages[0])

    def test_every_menu_action_is_connected(self):
        """スロット未接続のメニュー項目が残っていないこと。"""
        w = self._window()
        dead = []
        for menu_action in w.menuBar().actions():
            menu = menu_action.menu()
            if menu is None:
                continue
            for action in menu.actions():
                if action.isSeparator():
                    continue
                if not action.receivers(action.triggered):
                    dead.append(f"{menu_action.text()} > {action.text()}")
        self.assertEqual(dead, [], "スロット未接続のメニュー項目: %s" % dead)

    def test_settings_dialog_opens(self):
        w = self._window()
        with mock.patch("ui.main_window.SettingsDialog") as dialog_class:
            dialog_class.return_value.exec.return_value = 0
            w._on_settings()
        dialog_class.assert_called_once()
        self.assertIs(dialog_class.call_args.kwargs["config_manager"], w.config_manager)

    def test_settings_dialog_is_blocked_when_the_config_failed_to_load(self):
        """破損した config をデフォルト設定で上書きしないため。"""
        w = self._window()
        w.config_manager.load_error = "読み込みエラー"
        with mock.patch("ui.main_window.SettingsDialog") as dialog_class, \
             mock.patch("ui.main_window.QMessageBox.warning") as warn:
            w._on_settings()
        dialog_class.assert_not_called()
        warn.assert_called_once()

    def test_settings_dialog_reapplies_terminal_settings_on_accept(self):
        from PyQt6.QtWidgets import QDialog
        w = self._window()
        with mock.patch("ui.main_window.SettingsDialog") as dialog_class, \
             mock.patch.object(w, "_apply_terminal_settings_from_config") as reapply:
            dialog_class.return_value.exec.return_value = QDialog.DialogCode.Accepted
            w._on_settings()
        reapply.assert_called_once()


class PasteGuardTest(unittest.TestCase):
    """再接続待機中はペーストで送信しないこと。

    keyPressEvent は再接続待機中に Enter 以外を捨てるのに、custom_paste は
    _input_enabled しか見ておらず素通りしていた。切断済みの接続へ
    クリップボードの中身がそのまま流れる。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self, connected=True, reconnecting=False):
        from ui.terminal_widget import InteractiveTerminal
        terminal = InteractiveTerminal()
        terminal.set_input_enabled(connected)
        if reconnecting:
            terminal.set_reconnect_mode(True)
        return terminal

    def test_can_send_input_tracks_both_flags(self):
        self.assertTrue(self._terminal().can_send_input())
        self.assertFalse(self._terminal(connected=False).can_send_input())
        # 再接続モードは _input_enabled を True へ戻すので、それだけでは足りない
        self.assertFalse(self._terminal(reconnecting=True).can_send_input())

    def test_paste_sends_nothing_while_waiting_to_reconnect(self):
        from PyQt6.QtWidgets import QApplication
        terminal = self._terminal(reconnecting=True)
        self.assertTrue(terminal._input_enabled, "再接続モードは入力を有効へ戻す")
        sent = []
        terminal.key_pressed.connect(sent.append)
        QApplication.clipboard().setText("show running-config")
        terminal.custom_paste()
        self.assertEqual(sent, [])

    def test_paste_sends_while_connected(self):
        from PyQt6.QtWidgets import QApplication
        terminal = self._terminal()
        sent = []
        terminal.key_pressed.connect(sent.append)
        QApplication.clipboard().setText("show version")
        terminal.custom_paste()
        self.assertEqual("".join(sent), "show version")

    def test_context_menu_paste_is_disabled_while_waiting_to_reconnect(self):
        """右クリック経路も同じ条件で塞がっていること。"""
        from PyQt6.QtGui import QContextMenuEvent
        from PyQt6.QtCore import QPoint
        terminal = self._terminal(reconnecting=True)
        captured = {}

        def fake_exec(menu, *args, **kwargs):
            captured["items"] = [(a.text(), a.isEnabled()) for a in menu.actions()]
            return None

        with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
            terminal.contextMenuEvent(QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, QPoint(1, 1)))
        self.assertIn(("貼り付け", False), captured["items"])

    def test_menu_paste_explains_why_nothing_happened(self):
        """メニューからのペーストは理由を伝えること（無反応にしない）。"""
        w = MenuActionsTest._window(self)
        terminal = w.terminal_widget.create_terminal_tab("ルータA")
        terminal.set_input_enabled(True)
        terminal.set_reconnect_mode(True)
        messages = []
        w.status_bar.showMessage = lambda text, *a: messages.append(text)
        w._on_paste()
        self.assertEqual(len(messages), 1)
        self.assertIn("接続中", messages[0])


if __name__ == "__main__":
    unittest.main()
