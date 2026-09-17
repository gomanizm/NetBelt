"""機器名の一意性検査が、ツリーに並ぶ自動検出COMポート名まで見ることを検証する。

_device_name_conflict は config・self.connections・ターミナルタブの3つしか
見ていない。自動検出のCOMポートは第4の名前空間で、config には無い。

そのため自動検出の COM3 がツリーに出ている状態で、機器名 "COM3" の登録機器を
作れてしまう。あとからどちらか一方へ繋ぐと self.connections["COM3"] が埋まり、
もう一方は接続できず「COM3 は既に接続されています」という、実態と食い違う
案内だけが出る。

自動検出ポートと同じ名前は、追加・編集・複製のどれでも断ること。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

PORTS = [{"port": "COM3", "description": "USB Serial Port"}]


def _device(name="COM3"):
    return {"name": name, "host": "192.0.2.10", "port": 22, "protocol": "ssh",
            "username": "cisco", "password": "", "ssh_key": "", "macros": []}


class DetectedPortNamesTest(unittest.TestCase):
    """DeviceTree が、並べている自動検出ポート名を答えられること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _tree(self, ports):
        from ui.device_tree import DeviceTree
        with mock.patch("ui.device_tree.list_serial_ports",
                        return_value=ports):
            tree = DeviceTree()
            tree.load_from_config([])
        self.addCleanup(tree.deleteLater)
        return tree

    def test_detected_ports_are_listed(self):
        tree = self._tree(PORTS)
        self.assertEqual(tree.detected_port_names(), {"COM3"})

    def test_registered_devices_are_not_listed(self):
        """config 由来の機器は自動検出ではないので混ざらないこと。"""
        tree = self._tree([])
        tree_groups = [{"name": "Default", "devices": [_device("R1")],
                        "auto_commands": []}]
        with mock.patch("ui.device_tree.list_serial_ports", return_value=[]):
            tree.load_from_config(tree_groups)
        self.assertEqual(tree.detected_port_names(), set())


class DeviceNameConflictsWithAutodetectedPortsTest(unittest.TestCase):
    """自動検出ポートと同名の登録機器を作れないこと。"""

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

    def _window(self, ports=PORTS):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-autodetect-name-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
             mock.patch("ui.device_tree.list_serial_ports", return_value=ports):
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

    def test_adding_a_name_held_by_a_detected_port_is_refused(self):
        window = self._window()
        dialog = self._dialog(_device("COM3"))

        warn, add_device, _ = self._run(
            window, dialog, lambda: window._on_add_device())

        self.assertFalse(
            add_device.called,
            "自動検出ポートと同名の機器を登録できてしまう")
        warn.assert_called_once()
        self.assertIn("COM3", warn.call_args[0][2])

    def test_duplicating_into_a_detected_port_name_is_refused(self):
        window = self._window()
        dialog = self._dialog(_device("COM3"))

        warn, add_device, _ = self._run(
            window, dialog,
            lambda: window._on_device_duplicate("Default", _device("R1")))

        self.assertFalse(
            add_device.called,
            "自動検出ポートと同名へ複製できてしまう")
        warn.assert_called_once()

    def test_renaming_into_a_detected_port_name_is_refused(self):
        window = self._window()
        old = _device("R1")
        window.config_manager.add_device("Default", old)
        dialog = self._dialog(_device("COM3"))

        warn, _, update_device = self._run(
            window, dialog,
            lambda: window._on_device_edit("Default", old))

        self.assertFalse(
            update_device.called,
            "自動検出ポートと同名へ改名できてしまう")
        warn.assert_called_once()

    def test_an_unrelated_name_is_still_accepted(self):
        """検出ポートと無関係な名前は、これまで通り登録できること（回帰防止）。"""
        window = self._window()
        dialog = self._dialog(_device("R2"))

        warn, add_device, _ = self._run(
            window, dialog, lambda: window._on_add_device())

        self.assertTrue(add_device.called, "無関係な名前まで断っている")
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
