"""拒まれたボーレート変更で、シリアルポート一覧の作り直しが 1 回で済むことを検証する。

実測（8b0c94e）: 拒否 1 回につき DeviceTree.refresh_serial_ports() が 2 回
走っていた。_set_baudrate の中で 1 回、MainWindow._restore_serial_baudrate →
DeviceTree.restore_baudrate でもう 1 回。作り直しは実機のポート列挙
（list_serial_ports）とツリーの「コンソール接続」の作り直しを伴い、
そのたびに折りたたみ状態も戻る。検査役の記録では、余分な作り直しの目印として
`[INFO] シリアルポート追加を検出: COM3` が 1 行多く出ていた。

修正: 戻す側（restore_baudrate）は、_set_baudrate から呼ばれている間は値を
書くだけにして、作り直しは _set_baudrate が最後に 1 回だけ行う。接続できた
ときの経路（MainWindow._on_connection_success からの呼び出し）は _set_baudrate
の外なので、これまでどおり restore_baudrate が作り直す。ツリーの表示
（チェック）と再接続用の設定が実際の値に戻るという D16 の振る舞いは変えない。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

import serial

from PyQt6.QtCore import Qt

sys.path.insert(0, "src")

PORTS = [{"port": "COM3", "description": "USB Serial Port"}]


def _autodetected(port="COM3", baudrate=9600):
    """自動検出項目と同じ形の機器データを返す。"""
    return {"name": port, "type": "serial", "protocol": "serial", "port": port,
            "baudrate": baudrate, "description": "USB Serial Port",
            "source": "autodetect"}


class _PickyPort:
    """pyserial と同じ順で値を書き換え、refuse に挙げた値だけを拒む疑似ポート。"""

    def __init__(self, baudrate=9600, refuse=()):
        self._baudrate = baudrate
        self.refuse = set(refuse)
        self.is_open = True
        self.in_waiting = 0

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value):
        self._baudrate = value
        if value in self.refuse:
            raise serial.SerialException("SetCommState failed")

    def read(self, n):
        return b""

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


class BaudrateRefusalRebuildsOnceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.listed = []
        ports = mock.patch(
            "ui.device_tree.list_serial_ports",
            side_effect=lambda: self.listed.append(1) or [dict(p) for p in PORTS])
        ports.start()
        self.addCleanup(ports.stop)

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-baud-rebuild-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        self.addCleanup(self._dispose_connections, window)
        self.addCleanup(window.deleteLater)
        return window

    @staticmethod
    def _dispose_connections(window):
        for conn in list(window.connections.values()):
            conn.dispose()
        window.connections.clear()

    def _attach(self, window, port):
        """開いたポートを持つ COM3 の接続を、接続した直後と同じ形で置く。"""
        from core.serial_connection import SerialConnection
        conn = SerialConnection("COM3", 9600)
        self.addCleanup(conn.dispose)
        conn.serial_conn = port
        conn._is_connected = True
        window.terminal_widget.create_terminal_tab("COM3")
        window.connections["COM3"] = conn
        window.device_info["COM3"] = _autodetected("COM3", 9600)
        return conn

    def _set_baudrate(self, window, baudrate):
        """ツリーからボーレートを変え、(作り直しの回数, ポート列挙の回数) を返す。"""
        tree = window.device_tree
        self.listed.clear()
        with mock.patch.object(tree, "refresh_serial_ports",
                               wraps=tree.refresh_serial_ports) as rebuild:
            tree._set_baudrate("COM3", baudrate)
        return rebuild.call_count, len(self.listed)

    @staticmethod
    def _item_baudrate(window):
        """ツリーの COM3 の項目が持っているボーレート。"""
        root = window.device_tree.tree.invisibleRootItem()
        for i in range(root.childCount()):
            group = root.child(i)
            if group.text(0) == "コンソール接続":
                data = group.child(0).data(0, Qt.ItemDataRole.UserRole)
                return data.get("baudrate")
        return None

    def test_a_refusal_rebuilds_the_port_list_once(self):
        window = self._window()
        self._attach(window, _PickyPort(9600, refuse={115200}))

        rebuilds, listings = self._set_baudrate(window, 115200)

        self.assertEqual(rebuilds, 1, "拒否 1 回で一覧を作り直しすぎている")
        self.assertEqual(listings, 1, "ポートの列挙が 1 回で済んでいない")

    def test_a_refusal_still_restores_the_tree_and_the_reconnect(self):
        """D16 の振る舞い: 拒まれたら表示も再接続用の設定も実際の値へ戻ること。"""
        window = self._window()
        self._attach(window, _PickyPort(9600, refuse={115200}))

        self._set_baudrate(window, 115200)

        self.assertEqual(window.device_tree._serial_port_baudrates["COM3"], 9600)
        self.assertEqual(self._item_baudrate(window), 9600,
                         "ツリーの項目が拒まれた値のまま")
        self.assertEqual(window.device_info["COM3"]["baudrate"], 9600)

    def test_an_accepted_change_also_rebuilds_once(self):
        """対照: 受け付けられた変更の作り直しも 1 回のままであること。"""
        window = self._window()
        self._attach(window, _PickyPort(9600))

        rebuilds, listings = self._set_baudrate(window, 115200)

        self.assertEqual((rebuilds, listings), (1, 1))
        self.assertEqual(self._item_baudrate(window), 115200)

    def test_restoring_outside_a_change_still_rebuilds(self):
        """対照: 接続できたときの経路（_set_baudrate の外）は作り直すこと。"""
        window = self._window()
        tree = window.device_tree
        tree._serial_port_baudrates["COM3"] = 115200
        tree.refresh_serial_ports()
        self.assertEqual(self._item_baudrate(window), 115200, "前提が崩れている")

        tree.restore_baudrate("COM3", 9600)

        self.assertEqual(self._item_baudrate(window), 9600,
                         "ツリーが実際の値へ戻っていない")


if __name__ == "__main__":
    unittest.main()
