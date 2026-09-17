"""ボーレートの変更が、開いているシリアル接続へその場で効くことを検証する。

Cisco 機器で `speed 115200`（`terminal speed`）を実行すると、機器側の
コンソールはその時点で 115200 baud に切り替わる。NetBelt 側もツリーの
右クリックからボーレートを 115200 にするが、これまで変わるのはツリーの
設定と再接続用の写しだけで、開いている接続は旧ボーレートのままだった。
画面は文字化けし、利用者はタブを閉じて繋ぎ直す必要があった。

pyserial 3.5 は、開いたポートの baudrate へ代入すると SetCommState で
その場で設定し直す。ポートを開いたまま、読み取り・送信スレッドも
止めずに済むので、タブも接続もそのまま使い続けられる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

import serial

sys.path.insert(0, "src")

PORTS = [{"port": "COM3", "description": "USB Serial Port"},
         {"port": "COM4", "description": "USB Serial Port"}]


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


class _RefusingPort:
    """開いているが、ボーレートの変更を拒むポート。"""

    def __init__(self, baudrate=9600):
        self._baudrate = baudrate
        self.is_open = True
        self.in_waiting = 0

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value):
        raise serial.SerialException("SetCommState failed")

    def read(self, n):
        return b""

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


def open_connection(port_name="COM3", baudrate=9600, port=None):
    """開いたポートを持つ SerialConnection を作る（実機の COM は開かない）。"""
    from core.serial_connection import SerialConnection
    conn = SerialConnection(port_name, baudrate)
    conn.serial_conn = port if port is not None else serial.serial_for_url(
        "loop://", baudrate=baudrate, timeout=0)
    conn._is_connected = True
    return conn


class SerialConnectionBaudrateTest(unittest.TestCase):
    """SerialConnection 単体: 開いたポートへその場で反映すること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_open_port_takes_the_new_baudrate(self):
        conn = open_connection(baudrate=9600)
        port = conn.serial_conn
        self.addCleanup(conn.dispose)

        self.assertTrue(conn.set_baudrate(115200))

        self.assertIs(conn.serial_conn, port, "ポートを開き直している")
        self.assertTrue(port.is_open, "ポートを閉じている")
        self.assertEqual(port.baudrate, 115200, "開いているポートが旧ボーレートのまま")
        self.assertEqual(conn.baudrate, 115200)
        self.assertTrue(conn.is_connected)

    def test_a_refused_baudrate_keeps_the_session(self):
        """ポートが拒んだら、接続は保ったまま False を返すこと。

        error_occurred を出すと、MainWindow が接続を捨てて再接続待ちに
        入る。まだ使える接続を、ボーレートの変更失敗だけで切らない。
        """
        port = _RefusingPort(9600)
        conn = open_connection(baudrate=9600, port=port)
        self.addCleanup(conn.dispose)
        errors = []
        conn.error_occurred.connect(errors.append)

        self.assertFalse(conn.set_baudrate(115200))

        self.assertTrue(port.is_open)
        self.assertTrue(conn.is_connected)
        self.assertEqual(conn.baudrate, 9600, "反映できなかったのに新しい値を覚えている")
        self.assertEqual(errors, [], "接続エラーとして通知すると接続が捨てられる")

    def test_without_an_open_port_the_value_is_kept_for_the_next_connect(self):
        from core.serial_connection import SerialConnection
        conn = SerialConnection("COM3", 9600)

        self.assertTrue(conn.set_baudrate(38400))

        self.assertEqual(conn.baudrate, 38400)


class TreeBaudrateReachesTheOpenSessionTest(unittest.TestCase):
    """ツリーでボーレートを変えると、開いているタブの接続へ効くこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-livebaud-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
             mock.patch("ui.device_tree.list_serial_ports", return_value=PORTS):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        self.addCleanup(w.deleteLater)
        return w

    def _attach(self, w, port_name, baudrate=9600, port=None):
        conn = open_connection(port_name, baudrate, port)
        self.addCleanup(conn.dispose)
        w.terminal_widget.create_terminal_tab(port_name)
        w.connections[port_name] = conn
        w.device_info[port_name] = autodetected(port_name, baudrate)
        return conn

    def _set(self, w, port_name, baudrate):
        with mock.patch("ui.device_tree.list_serial_ports", return_value=PORTS):
            w.device_tree._set_baudrate(port_name, baudrate)

    def test_the_open_session_switches_without_reconnecting(self):
        w = self._window()
        conn = self._attach(w, "COM3", 9600)
        port = conn.serial_conn

        self._set(w, "COM3", 115200)

        self.assertEqual(port.baudrate, 115200,
                         "開いている接続が旧ボーレートのまま（タブを閉じて繋ぎ直す必要がある）")
        self.assertIs(w.connections.get("COM3"), conn, "接続を作り直している")
        self.assertIs(conn.serial_conn, port, "ポートを開き直している")
        self.assertTrue(conn.is_connected)
        self.assertTrue(w.terminal_widget.has_terminal("COM3"), "タブが閉じている")

    def test_another_ports_session_is_left_alone(self):
        w = self._window()
        self._attach(w, "COM3", 9600)
        other = self._attach(w, "COM4", 9600)

        self._set(w, "COM3", 115200)

        self.assertEqual(other.serial_conn.baudrate, 9600)

    def test_a_refused_change_keeps_the_session_and_says_so(self):
        w = self._window()
        port = _RefusingPort(9600)
        conn = self._attach(w, "COM3", 9600, port=port)

        self._set(w, "COM3", 115200)

        self.assertIs(w.connections.get("COM3"), conn, "変更の失敗だけで接続を捨てている")
        self.assertTrue(conn.is_connected)
        self.assertFalse(w.terminal_widget._terminals["COM3"]._reconnect_mode,
                         "まだ使える接続なのに再接続待ちにしている")
        self.assertIn("ボーレート", w.status_bar.currentMessage(),
                      "変更できなかったことを知らせていない")


if __name__ == "__main__":
    unittest.main()
