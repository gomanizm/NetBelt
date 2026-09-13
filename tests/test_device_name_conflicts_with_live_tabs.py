"""機器名の一意性検査が、開いているタブと生きている接続まで見ることを検証する。

改名すると config からは旧名が消えるが、self.connections と terminal_widget
のタブ名は旧名のまま残る。一意性検査は config しか見ていないので、旧名を
別の機器へ付け直せてしまう。すると _find_group_of_device が旧名を新しい
機器のグループへ解決し、そのグループの自動実行コマンドが、まだ生きている
前の接続（別の機器）へ飛ぶ。

config に無くてもタブ・接続が使っている名前は、追加・編集・複製のどれでも
断ること。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _device(name="Ubuntu0"):
    return {"name": name, "host": "192.0.2.10", "port": 22, "protocol": "ssh",
            "username": "cisco", "password": "", "ssh_key": "", "macros": []}


class DeviceNameConflictsWithLiveTabsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 破棄済みウィジェットへのシグナル配送で落ちるため保持する
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-nameconflict-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _dialog(self, device_data, group="Default"):
        from PyQt6.QtWidgets import QDialog
        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = device_data
        dialog.get_selected_group.return_value = group
        dialog.group_combo.findText.return_value = 0
        return dialog

    def _run(self, window, dialog, action):
        """ダイアログを差し替えて action を実行し、(警告文, 保存呼び出し) を返す。"""
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn, \
             mock.patch.object(window, "_load_devices"), \
             mock.patch.object(window.config_manager, "add_device",
                               return_value=True) as add_device, \
             mock.patch.object(window.config_manager, "update_device",
                               return_value=True) as update_device:
            action()
        return warn, add_device, update_device

    def _open_tab(self, window, device_name):
        window.terminal_widget.create_terminal_tab(device_name)

    def test_adding_a_name_held_by_a_live_connection_is_refused(self):
        """接続中のセッションが使っている名前では機器を追加できないこと。"""
        window = self._window()
        window.connections["Ubuntu0"] = object()
        self.addCleanup(window.connections.pop, "Ubuntu0", None)
        dialog = self._dialog(_device())

        warn, add_device, _ = self._run(
            window, dialog, lambda: window._on_add_device())

        self.assertFalse(add_device.called,
                         "接続中の名前で別の機器を登録できてしまう")
        warn.assert_called_once()
        self.assertIn("Ubuntu0", warn.call_args[0][2])

    def test_adding_a_name_held_by_an_open_tab_is_refused(self):
        """開いたままのタブが使っている名前では機器を追加できないこと。"""
        window = self._window()
        self._open_tab(window, "Ubuntu0")
        dialog = self._dialog(_device())

        warn, add_device, _ = self._run(
            window, dialog, lambda: window._on_add_device())

        self.assertFalse(add_device.called,
                         "開いているタブの名前で別の機器を登録できてしまう")
        warn.assert_called_once()

    def test_duplicating_into_a_name_held_by_an_open_tab_is_refused(self):
        """複製でも同じこと。"""
        window = self._window()
        self._open_tab(window, "Ubuntu0")
        dialog = self._dialog(_device())

        warn, add_device, _ = self._run(
            window, dialog,
            lambda: window._on_device_duplicate("Default", _device("Cat8000v")))

        self.assertFalse(add_device.called,
                         "開いているタブの名前で複製を登録できてしまう")
        warn.assert_called_once()

    def test_renaming_into_a_name_held_by_an_open_tab_is_refused(self):
        """編集で、タブが使っている別の名前へ改名できないこと。"""
        window = self._window()
        self._open_tab(window, "Ubuntu0")
        old = _device("Cat8000v")
        window.config_manager.add_device("Default", old)
        dialog = self._dialog(_device("Ubuntu0"))

        warn, _, update_device = self._run(
            window, dialog,
            lambda: window._on_device_edit("Default", old))

        self.assertFalse(update_device.called,
                         "開いているタブの名前へ改名できてしまう")
        warn.assert_called_once()

    def test_editing_a_connected_device_keeps_working(self):
        """接続中の機器を、名前を変えずに編集できること（回帰防止）。"""
        window = self._window()
        old = _device("Ubuntu0")
        window.config_manager.add_device("Default", old)
        self._open_tab(window, "Ubuntu0")
        window.connections["Ubuntu0"] = object()
        self.addCleanup(window.connections.pop, "Ubuntu0", None)
        dialog = self._dialog(dict(old, password="new-secret"))

        warn, _, update_device = self._run(
            window, dialog,
            lambda: window._on_device_edit("Default", old))

        self.assertTrue(update_device.called,
                        "接続中の機器を編集できなくなっている")
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
