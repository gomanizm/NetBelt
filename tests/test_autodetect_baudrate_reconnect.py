"""自動検出COMポートのボーレート変更が、Enter 再接続にも届くことを検証する。

DeviceTree._set_baudrate はツリー項目を作り直すだけなので、右クリックで
ボーレートを変えても、MainWindow が初回接続時に取った device_info の写しは
旧値のまま残る。Enter による再接続は device_info を読むため、接続ボタンや
ダブルクリック（ツリー項目を読む）と結果が食い違い、コンソール出力が
文字化けする。ボーレートの変更を MainWindow まで伝えること。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

PORTS = [{"port": "COM3", "description": "USB Serial Port"}]


def autodetected(port="COM3", baudrate=9600):
    """自動検出項目と同じ形の機器データを返す。"""
    return {
        "name": port,
        "type": "serial",
        "protocol": "serial",
        "port": port,
        "baudrate": baudrate,
        "description": "USB Serial Port",
        "source": "autodetect",
    }


class BaudrateChangeReachesDeviceInfoTest(unittest.TestCase):
    """ボーレート変更後、再接続に使う写しも新しい値になること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-baud-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
             mock.patch("ui.device_tree.list_serial_ports", return_value=PORTS):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        self.addCleanup(w.deleteLater)
        return w

    def test_reconnect_copy_follows_the_new_baudrate(self):
        w = self._window()
        w.device_info["COM3"] = autodetected(baudrate=9600)
        with mock.patch("ui.device_tree.list_serial_ports", return_value=PORTS):
            w.device_tree._set_baudrate("COM3", 115200)
        self.assertEqual(
            w.device_info["COM3"].get("baudrate"), 115200,
            "再接続用の写しが旧ボーレートのままで、Enter 再接続だけ古い値になる")

    def test_reconnect_uses_the_new_baudrate(self):
        """_reconnect_device が渡す機器データにも新しい値が載ること。"""
        w = self._window()
        w.device_info["COM3"] = autodetected(baudrate=9600)
        with mock.patch("ui.device_tree.list_serial_ports", return_value=PORTS):
            w.device_tree._set_baudrate("COM3", 38400)
        with mock.patch.object(w, "_on_connect_requested") as connect, \
             mock.patch.object(w.terminal_widget, "show_notice"):
            w._reconnect_device("COM3")
        connect.assert_called_once()
        self.assertEqual(connect.call_args[0][0].get("baudrate"), 38400)

    def test_a_registered_device_is_not_touched(self):
        """config 由来の機器の写しは、ツリーのボーレート設定で書き換えない。"""
        w = self._window()
        registered = autodetected(baudrate=9600)
        registered["name"] = "コンソールサーバ"
        del registered["source"]
        w.device_info["コンソールサーバ"] = registered
        with mock.patch("ui.device_tree.list_serial_ports", return_value=PORTS):
            w.device_tree._set_baudrate("COM3", 115200)
        self.assertEqual(w.device_info["コンソールサーバ"].get("baudrate"), 9600)

    def test_an_unknown_port_is_harmless(self):
        """まだ繋いでいないポートのボーレートを変えても落ちないこと。"""
        w = self._window()
        with mock.patch("ui.device_tree.list_serial_ports", return_value=PORTS):
            w.device_tree._set_baudrate("COM99", 115200)
        self.assertNotIn("COM99", w.device_info)
