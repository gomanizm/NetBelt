"""マクロ設定ダイアログの「状態」表示が、要求ではなく実際の開始に従うことを検証する。

何が起きていたか（実測）: 切断済みのセッションへキープアライブを立てない
修正は、MacroDialog へ渡す connected（開いた時点の接続状態）で「開始」を
灰色にしていた。ところが connected は開いた時点の値なので、ダイアログを
開いたあとに機器側が切れた場合は「開始」が有効のまま残る。押すと
MainWindow._start_keepalive は正しく断る（タイマーは立たず、ステータスバーに
「rtrA: 接続が切れているためキープアライブを開始できません」）のに、

    macro_manager.is_keepalive_active('rtrA'): False
    dialog.keepalive_status_label.text(): '状態: 動作中（60秒間隔）'

となり、この修正が消そうとした「押すと表示だけが進む見た目」が残っていた。
MacroDialog._on_keepalive_start が emit のあと無条件に
_update_keepalive_ui(True) を呼んでいたためで、メニューバーのマクロ設定
（_on_macro_settings）でもタブのマクロ設定（_on_macro_settings_from_context）
でも同じだった。

どう直したか: ダイアログの表示を「要求を出したこと」ではなく「始まったこと」
で進める。MacroDialog._on_keepalive_start / _on_keepalive_stop は要求を
emit するだけにし、表示の更新は MainWindow 側で macro_manager の実状態
（is_keepalive_active）を見てから行う。
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


class KeepaliveDisplayFollowsTheRealStartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-keepalive-display-")
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
        """窓を閉じ、溜まった出力の配り直しを止める（隣の同種テストと同じ形）

        ここで deleteLater を予約して破棄まで走らせない。この試験一式は
        QApplication を使い回すので、まだ配り終えていないイベントを抱えた
        まま C++ オブジェクトを壊すと、後のテストの processEvents で
        access violation になる（conftest の注意を参照）。
        """
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
        self.addCleanup(window.macro_manager.cleanup_device, "rtrA")
        return window, device

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

    def _drop(self, window):
        """開いている最中に相手機器から切断される（タブは残る）"""
        FakeSSH.instances[-1].disconnected.emit()
        self._pump()
        self.assertNotIn("rtrA", window.connections, "前提: 切断されている")

    def _assert_display_is_stopped(self, dialog):
        self.assertEqual(dialog.keepalive_status_label.text(), "状態: 停止中",
                         "始まっていないのに表示だけが進んだ: %r"
                         % dialog.keepalive_status_label.text())
        self.assertFalse(dialog.keepalive_active,
                         "ダイアログが動作中だと思い込んでいる")
        self.assertFalse(dialog.keepalive_stop_btn.isEnabled(),
                         "止めるものが無いのに「停止」が押せる")

    def test_the_display_does_not_advance_when_the_tab_dialog_start_is_refused(self):
        """タブのマクロ設定を開いたあとに切れたら、「開始」で表示だけが進まないこと。"""
        window, _device = self._connected_window()
        dialog = self._open_dialog(window, from_tab=True)
        self.assertTrue(dialog.keepalive_start_btn.isEnabled(),
                        "前提: 開いた時点では接続中なので「開始」が押せる")
        self._drop(window)

        dialog._on_keepalive_start()
        self._pump(0.1)

        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"),
                         "前提: 切断済みなので実際には始まっていない")
        self._assert_display_is_stopped(dialog)

    def test_the_display_does_not_advance_when_the_menu_dialog_start_is_refused(self):
        """メニューバーのマクロ設定から開いた場合も同じであること。"""
        window, _device = self._connected_window()
        dialog = self._open_dialog(window)
        self._drop(window)

        dialog._on_keepalive_start()
        self._pump(0.1)

        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"),
                         "前提: 切断済みなので実際には始まっていない")
        self._assert_display_is_stopped(dialog)

    def test_the_display_advances_when_the_start_really_happens(self):
        """繋がっている間は、これまでどおり「動作中」へ進むこと。"""
        window, _device = self._connected_window()
        dialog = self._open_dialog(window)

        dialog._on_keepalive_start()
        self._pump(0.1)

        self.assertTrue(window.macro_manager.is_keepalive_active("rtrA"),
                        "前提: 接続中なので実際に始まっている")
        self.assertEqual(dialog.keepalive_status_label.text(),
                         "状態: 動作中（60秒間隔）",
                         "始まったのに表示が進んでいない: %r"
                         % dialog.keepalive_status_label.text())
        self.assertTrue(dialog.keepalive_stop_btn.isEnabled(),
                        "動作中なのに「停止」が押せない")
        self.assertFalse(dialog.keepalive_interval_spin.isEnabled(),
                         "動作中なのに送信間隔を触れる")

    def test_the_display_goes_back_to_stopped_when_the_stop_really_happens(self):
        """繋がっている間の「停止」も、これまでどおり表示が戻ること。"""
        window, _device = self._connected_window()
        dialog = self._open_dialog(window)
        dialog._on_keepalive_start()
        self._pump(0.1)

        dialog._on_keepalive_stop()
        self._pump(0.1)

        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"),
                         "前提: 実際に止まっている")
        self._assert_display_is_stopped(dialog)
        self.assertTrue(dialog.keepalive_start_btn.isEnabled(),
                        "止めたあとに「開始」が押せない")


if __name__ == "__main__":
    unittest.main()
