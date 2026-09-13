"""自動検出したCOMポートを、登録機器と同じ名前空間で扱わないことを検証する。

device_tree は検出したシリアルポートを name=ポート名 の機器データとして
ツリーへ並べる。この項目は config に無いので、機器追加時の名前の一意性
検査（find_device_group）の対象外になり、同じ名前の登録機器と共存できる。

その状態で COM ポートへ繋ぐと、_find_group_of_device が config 側の
同名機器を見つけ、そのグループの自動実行コマンドがシリアルコンソールへ
そのまま流れる。プロトコルの違い（ssh / serial）も見ていない。
コンソールの先は機器の素のCLIなので、別機器向けの設定コマンドが
意図しない相手で走ることになる。

自動検出の項目には印を付け、印のある接続では自動実行コマンドを
始めないこと。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

PORTS = [{"port": "COM3", "description": "USB Serial Port"}]


class AutodetectedPortsAreMarkedTest(unittest.TestCase):
    """ツリーに並ぶ自動検出項目に、config 由来でない印が付くこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _tree(self):
        from PyQt6.QtCore import Qt
        from ui.device_tree import DeviceTree
        with mock.patch("ui.device_tree.list_serial_ports",
                        return_value=PORTS):
            tree = DeviceTree()
            tree.load_from_config([])
        self.addCleanup(tree.deleteLater)
        console_group = tree.tree.topLevelItem(tree.tree.topLevelItemCount() - 1)
        item = console_group.child(0)
        return item.data(0, Qt.ItemDataRole.UserRole)

    def test_the_item_carries_the_autodetect_mark(self):
        data = self._tree()
        self.assertEqual(data.get("name"), "COM3", "前提: 検出したポートが並ぶ")
        self.assertEqual(
            data.get("source"), "autodetect",
            "自動検出の項目に印が無く、config 由来の機器と区別できない")


class AutoCommandsSkipAutodetectedPortsTest(unittest.TestCase):
    """同名の登録機器があっても、COM ポートへ自動実行コマンドを流さないこと。"""

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
        d = tempfile.mkdtemp(prefix="netbelt-autodetect-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
             mock.patch("ui.device_tree.list_serial_ports", return_value=PORTS):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        self.assertTrue(window.config_manager.add_group("ラボA", ["conf t"]),
                        "前提: グループを作れた")
        self.assertTrue(window.config_manager.add_device(
            "ラボA", {"name": "COM3", "host": "192.0.2.10", "port": 22,
                      "protocol": "ssh", "username": "u", "password": ""}),
            "前提: 同名の機器を登録できた")
        return window

    def _auto_commands_started(self, window, device_info):
        """device_info の写しで接続中として _run_auto_commands を通す。"""
        from PyQt6.QtCore import QTimer
        window.connections["COM3"] = object()
        self.addCleanup(window.connections.pop, "COM3", None)
        window.device_info["COM3"] = device_info
        with mock.patch.object(QTimer, "singleShot") as single_shot:
            window._run_auto_commands("COM3")
        return single_shot.called

    def test_an_autodetected_port_does_not_run_group_auto_commands(self):
        window = self._window()
        started = self._auto_commands_started(
            window, {"name": "COM3", "type": "serial", "protocol": "serial",
                     "port": "COM3", "baudrate": 9600,
                     "source": "autodetect"})
        self.assertFalse(
            started,
            "別機器のグループ自動コマンドがシリアルコンソールへ流れる")

    def test_a_registered_device_still_runs_them(self):
        window = self._window()
        started = self._auto_commands_started(
            window, {"name": "COM3", "host": "192.0.2.10", "port": 22,
                     "protocol": "ssh", "username": "u", "password": ""})
        self.assertTrue(
            started, "登録機器の自動実行コマンドまで止まっている")


if __name__ == "__main__":
    unittest.main()
