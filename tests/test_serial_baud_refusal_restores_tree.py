"""ポートがボーレートの変更を拒んだら、接続先リストの表示と再接続用の設定を実際の値へ戻すことを検証する。

何が起きていたか（実測）: 自動検出の COM3（9600 baud）へ繋いでいる途中で、
接続先リストのボーレートを 115200 に変え、ポートがそれを拒むと、接続は
9600 baud のまま（端末にも「ボーレートを 115200 baud に変更できなかったため、
9600 baud のままです」と出る）なのに、ツリーの _serial_port_baudrates と
MainWindow の device_info['baudrate'] は 115200 のまま残った。そのあと切断されて
Enter で繋ぎ直すと 115200 で開こうとして「接続失敗: could not open port:
SetCommState failed」になった。接続済みのポートが拒んだ場合も、ツリーの
チェックと device_info は 115200 のままだった。

利用者の決定（2026-09-20）: ポートが変更を拒んだら、ツリーの表示（チェック）と
再接続用の設定を実際の（元の）値へ戻す。開いている途中で拒まれた場合
（SerialConnection.connect が元の速度へ戻す経路）も同じ。

実装: MainWindow._restore_serial_baudrate() が、自動検出の機器について
device_info['baudrate'] とツリー（DeviceTree.restore_baudrate、
serial_baudrate_changed は出さない）を接続の実際の値（conn.baudrate）へ
戻す。接続済みで set_baudrate が False を返したときと、接続できたとき
（_on_connection_success。開いている途中の変更が拒まれていれば、
connect() が conn.baudrate を開いたときの値へ戻している）に呼ぶ。
"""
import os
import sys
import tempfile
import threading
import time
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


class BaudRefusalRestoresTreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def setUp(self):
        ports = mock.patch("ui.device_tree.list_serial_ports",
                           side_effect=lambda: [dict(p) for p in PORTS])
        ports.start()
        self.addCleanup(ports.stop)

    def _pump(self, seconds=0.5):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-baud-restore-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        self.addCleanup(self._dispose_connections, window)
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
        conn.serial_conn = port
        conn._is_connected = True
        window.terminal_widget.create_terminal_tab("COM3")
        window.connections["COM3"] = conn
        window.device_info["COM3"] = _autodetected("COM3", 9600)
        return conn

    def _connect_changing_baud_while_opening(self, window, refuse):
        """COM3 を開いている最中に、接続先リストで 115200 を選ぶ。開いたポートを返す。"""
        opening = threading.Event()
        proceed = threading.Event()
        self.addCleanup(proceed.set)
        created = []

        def slow_serial(**kwargs):
            opening.set()
            proceed.wait(5.0)
            port = _PickyPort(kwargs["baudrate"], refuse=refuse)
            created.append(port)
            return port

        with mock.patch("core.serial_connection.serial.Serial",
                        side_effect=slow_serial):
            window._on_connect_requested(_autodetected("COM3", 9600))
            self.assertTrue(opening.wait(5.0), "前提: ポートを開き始めていない")
            window.device_tree._set_baudrate("COM3", 115200)
            proceed.set()
            end = time.time() + 5.0
            terminal = window.terminal_widget._terminals["COM3"]
            while not terminal.can_send_input() and time.time() < end:
                self._pump(0.05)
        self.assertTrue(terminal.can_send_input(), "前提: 接続できていない")
        self.assertEqual(len(created), 1, "前提: ポートが開いていない")
        return created[0]

    def _tree_state(self, window):
        """(ツリーが覚えている値, 項目の値, メニューでチェックが付いている値) を返す。"""
        tree = window.device_tree
        root = tree.tree.invisibleRootItem()
        item = None
        for i in range(root.childCount()):
            group = root.child(i)
            if group.text(0) == "コンソール接続":
                item = group.child(0)
        self.assertIsNotNone(item, "前提: 自動検出の COM3 がツリーに無い")
        checked = []

        def fake_exec(menu, *args, **kwargs):
            baud = next(a.menu() for a in menu.actions()
                        if a.text() == "ボーレート(Baud Rate)")
            checked.extend(a.text() for a in baud.actions() if a.isChecked())
            return None

        pos = tree.tree.visualItemRect(item).center()
        with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
            tree._show_context_menu(pos)
        return (tree._serial_port_baudrates.get("COM3"),
                item.data(0, Qt.ItemDataRole.UserRole).get("baudrate"),
                checked)

    def _reconnect_baudrate(self, window):
        """Enter で繋ぎ直したときに使うボーレート"""
        with mock.patch.object(window, "_on_connect_requested") as connect, \
                mock.patch.object(window.terminal_widget, "show_notice"):
            window._reconnect_device("COM3")
        connect.assert_called_once()
        return connect.call_args[0][0].get("baudrate")

    def test_a_refusal_on_an_open_port_restores_the_tree_and_the_reconnect(self):
        """接続済みのポートが拒んだら、ツリーと再接続用の設定を元の値へ戻すこと。"""
        window = self._window()
        port = _PickyPort(9600, refuse={115200})
        conn = self._attach(window, port)

        window.device_tree._set_baudrate("COM3", 115200)

        self.assertEqual(conn.baudrate, 9600, "前提: 接続は 9600 のまま")
        self.assertEqual(self._tree_state(window), (9600, 9600, ["9600 baud"]),
                         "ツリーが拒まれた値を示したまま")
        self.assertEqual(window.device_info["COM3"]["baudrate"], 9600,
                         "再接続用の設定が拒まれた値のまま")
        self.assertEqual(self._reconnect_baudrate(window), 9600)
        self.assertIn("変更できませんでした", window.status_bar.currentMessage())

    def test_a_refusal_while_opening_restores_the_tree_and_the_reconnect(self):
        """開いている途中の変更が拒まれた場合も、ツリーと再接続用の設定を元へ戻すこと。"""
        window = self._window()
        port = self._connect_changing_baud_while_opening(window, refuse={115200})

        self.assertEqual(port.baudrate, 9600, "前提: ポートは 9600 のまま")
        self.assertEqual(window.connections["COM3"].baudrate, 9600)
        self.assertEqual(self._tree_state(window), (9600, 9600, ["9600 baud"]),
                         "ツリーが拒まれた値を示したまま")
        self.assertEqual(window.device_info["COM3"]["baudrate"], 9600,
                         "再接続用の設定が拒まれた値のまま")
        self.assertEqual(self._reconnect_baudrate(window), 9600)

    def test_accepted_changes_keep_the_new_value(self):
        """対照: 受け付けられた変更は、ツリーにも再接続用の設定にも残ること。"""
        with self.subTest("接続済み"):
            window = self._window()
            self._attach(window, _PickyPort(9600))
            window.device_tree._set_baudrate("COM3", 115200)
            self.assertEqual(self._tree_state(window),
                             (115200, 115200, ["115200 baud"]))
            self.assertEqual(window.device_info["COM3"]["baudrate"], 115200)

        with self.subTest("開いている途中"):
            window = self._window()
            port = self._connect_changing_baud_while_opening(window, refuse=())
            self.assertEqual(port.baudrate, 115200)
            self.assertEqual(self._tree_state(window),
                             (115200, 115200, ["115200 baud"]))
            self.assertEqual(window.device_info["COM3"]["baudrate"], 115200)


if __name__ == "__main__":
    unittest.main()
