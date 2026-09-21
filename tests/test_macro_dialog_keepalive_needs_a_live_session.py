"""マクロ設定ダイアログからも、切断済みのセッションへキープアライブを始めないことを検証する。

何が起きていたか（実測）: 切断済みのセッションを守っていたのは「ツール」
（DeviceTree の keepalive_start_requested / keepalive_stop_requested）の
受け口だけで、マクロ設定ダイアログの経路が残っていた。切断してもタブは
残るので、メニューバーの「ツール→マクロ設定(&M)」もタブの右クリックからの
マクロ設定も、切断済みのタブ名でダイアログを開ける。ダイアログの
「開始」は接続を見ておらず、keepalive_start_requested が
MainWindow._start_keepalive へ直結しているため、

    keepalive active after macro dialog start: True
    new conn sent: ['\\r']   ← 同じ名前で繋ぎ直すと新セッションへ CR

となり、「ツール」で塞いだはずの症状がそのまま再現した。ダイアログ側も
押せば _update_keepalive_ui(True) で「動作中」の表示だけが進んでいた。

どう直したか: 3 つの入口（「ツール」・メニューバーのマクロ設定・タブの
マクロ設定）が必ず通る MainWindow._start_keepalive / _stop_keepalive の
先頭へ、接続が生きているかの確認（_session_is_live）を移した。あわせて
MacroDialog へ connected を渡し、接続が無いときは「開始」を最初から
無効にして、押せてしまう見た目をなくした。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, pyqtSignal

sys.path.insert(0, "src")


class FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。connect() は即座に成功を知らせる。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []
        FakeSSH.instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        self.sent.append(command)

    def dispose(self):
        pass

    def disconnect(self):
        self.disconnected.emit()


class MacroDialogKeepaliveNeedsALiveSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-macro-keepalive-")
        FakeSSH.instances = []
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir)),
                mock.patch("ui.main_window.SSHConnection", FakeSSH)):
            patch.start()
            self.addCleanup(patch.stop)

    def _pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def _discard(window):
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _connected_window(self):
        """rtrA へ接続済みのメインウィンドウと、その機器データ"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.dir, "config.json"))
            window = MainWindow()
        self.addCleanup(self._discard, window)
        device = {"name": "rtrA", "host": "192.0.2.10", "port": 22,
                  "protocol": "ssh", "username": "u", "password": "",
                  "ssh_key": "", "macros": []}
        self.assertTrue(window.config_manager.add_device("Default", dict(device)),
                        "前提: 機器を登録できた")
        window._load_devices()
        window._on_connect_requested(device)
        self._pump()
        self.assertIn("rtrA", window.connections, "前提: 接続できている")
        return window, device

    def _drop(self, window):
        """相手機器から切断される（タブは残る）"""
        FakeSSH.instances[-1].disconnected.emit()
        self._pump()
        self.assertNotIn("rtrA", window.connections, "前提: 切断されている")
        self.assertEqual(window.terminal_widget.get_current_tab_name(), "rtrA",
                         "前提: 切断してもタブは残る")

    def _open_dialog(self, window, from_tab=False):
        """マクロ設定ダイアログを開いたところで止めて、そのダイアログを返す"""
        from ui.main_window import MainWindow
        captured = []
        with mock.patch.object(MainWindow, "_exec_dialog",
                               lambda self, dialog: captured.append(dialog)):
            if from_tab:
                window._on_macro_settings_from_context("rtrA")
            else:
                window._on_macro_settings()
        self.assertEqual(len(captured), 1, "前提: マクロ設定ダイアログが開いた")
        self.addCleanup(captured[0].deleteLater)
        return captured[0]

    def test_the_menu_bar_dialog_does_not_start_keepalive_after_the_drop(self):
        """メニューバーのマクロ設定からは、切断済みのタブでキープアライブを始めないこと。"""
        window, _device = self._connected_window()
        self._drop(window)
        dialog = self._open_dialog(window)

        dialog._on_keepalive_start()
        self._pump(0.1)

        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"),
                         "切断済みのセッションにキープアライブのタイマーが立った")
        self.assertFalse(
            window.terminal_widget.tools_state_for("rtrA")["keepalive_active"],
            "タブ側にも「動作中」の印が付いた")
        message = window.status_bar.currentMessage()
        self.assertIn("rtrA", message, "どの機器の話か分からない: %r" % message)
        self.assertIn("接続", message,
                      "切れていて実行できないことを知らせていない: %r" % message)

    def test_no_cr_reaches_a_new_session_with_the_same_name(self):
        """止め損ねたタイマーが、繋ぎ直した新しいセッションへ CR を送らないこと。"""
        window, device = self._connected_window()
        self._drop(window)
        dialog = self._open_dialog(window)
        dialog._on_keepalive_start()
        self._pump(0.1)

        window._on_connect_requested(device)
        self._pump()
        timer = window.macro_manager._keepalive_timers.get("rtrA")
        if timer is not None:
            timer.timeout.emit()
            self._pump(0.1)

        self.assertEqual(FakeSSH.instances[-1].sent, [],
                         "新しいセッションへキープアライブの CR が送られた")

    def test_the_tab_dialog_does_not_start_keepalive_after_the_drop(self):
        """タブの右クリックからのマクロ設定も、同じく始めないこと。"""
        window, _device = self._connected_window()
        self._drop(window)
        dialog = self._open_dialog(window, from_tab=True)

        dialog._on_keepalive_start()
        self._pump(0.1)

        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"),
                         "切断済みのセッションにキープアライブのタイマーが立った")

    def test_the_start_button_is_disabled_while_the_session_is_gone(self):
        """切断済みのタブで開いたダイアログは、「開始」を最初から押せないこと。"""
        window, _device = self._connected_window()
        self._drop(window)
        dialog = self._open_dialog(window)

        self.assertFalse(dialog.keepalive_start_btn.isEnabled(),
                         "押せば「動作中」の表示だけが進むボタンが有効のまま")

    def test_stopping_from_the_dialog_after_the_drop_is_ignored(self):
        """切断済みのタブでの「停止」も、黙って通さずに知らせること。"""
        window, _device = self._connected_window()
        window._start_keepalive("rtrA", 60)
        self.assertTrue(window.macro_manager.is_keepalive_active("rtrA"),
                        "前提: 接続中は開始できる")
        self._drop(window)
        dialog = self._open_dialog(window)
        window.status_bar.clearMessage()

        with mock.patch.object(window.terminal_widget,
                               "set_keepalive_status") as status:
            dialog._on_keepalive_stop()
            self._pump(0.1)

        status.assert_not_called()
        message = window.status_bar.currentMessage()
        self.assertIn("接続", message,
                      "切れていて実行できないことを知らせていない: %r" % message)

    def test_the_dialog_still_starts_keepalive_while_connected(self):
        """繋がっている間は、これまでどおりダイアログから開始・停止できること。"""
        window, _device = self._connected_window()
        dialog = self._open_dialog(window)

        self.assertTrue(dialog.keepalive_start_btn.isEnabled(),
                        "接続中なのに「開始」が灰色になっている")
        dialog._on_keepalive_start()
        self._pump(0.1)
        self.assertTrue(window.macro_manager.is_keepalive_active("rtrA"),
                        "接続中なのにキープアライブを開始できない")

        dialog._on_keepalive_stop()
        self._pump(0.1)
        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"),
                         "接続中なのにキープアライブを止められない")


if __name__ == "__main__":
    unittest.main()
