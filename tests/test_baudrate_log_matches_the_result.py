"""ボーレート変更の開発者向けログが、実際の結果と食い違わないことを検証する。

実測（8b0c94e）: DeviceTree._set_baudrate は、ポートが受け付けるか分からない
段階で無条件に `[INFO] COM3 のボーレートを 115200 baud に設定しました` を
出していた。ポートが拒むと、そのあとに
`[Serial] ボーレートを変更できませんでした: SetCommState failed` が続き、
ツリーと再接続用の設定は実際の 9600 へ戻るのに、先に出た「設定しました」
だけが残る。ログを読むと 115200 に設定できたように見える。

修正: 変更を伝える先（MainWindow）が済んでから、ツリーが覚えている値を
見て文言を決める。拒まれていれば restore_baudrate が実際の値を書き戻して
いるので、同じ場所へ「変更できませんでした（… のままです）」を 1 行出す。
開発者向けのログだけの問題なので、利用者向けの表示は変えない。
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import serial

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


class BaudrateLogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ports = mock.patch("ui.device_tree.list_serial_ports",
                           side_effect=lambda: [dict(p) for p in PORTS])
        ports.start()
        self.addCleanup(ports.stop)

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-baud-log-")
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

    @staticmethod
    def _set_baudrate(window, baudrate):
        """ツリーからボーレートを変え、その間に出た標準出力を返す。"""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            window.device_tree._set_baudrate("COM3", baudrate)
        return buffer.getvalue()

    def test_a_refused_change_is_not_logged_as_applied(self):
        """拒まれたら「設定しました」と残さないこと。"""
        window = self._window()
        conn = self._attach(window, _PickyPort(9600, refuse={115200}))

        text = self._set_baudrate(window, 115200)

        self.assertEqual(conn.baudrate, 9600, "前提: 接続は 9600 のまま")
        self.assertNotIn("設定しました", text,
                         "拒まれたのに設定できたと記録している: %r" % text)

    def test_a_refused_change_is_logged_where_it_was_requested(self):
        """拒まれたことが、同じ場所に 1 行残ること。"""
        window = self._window()
        self._attach(window, _PickyPort(9600, refuse={115200}))

        text = self._set_baudrate(window, 115200)

        lines = [line for line in text.splitlines()
                 if line.startswith("[INFO] COM3 のボーレート")]
        self.assertEqual(len(lines), 1, "記録が 1 行ではない: %r" % text)
        self.assertIn("115200", lines[0])
        self.assertIn("できませんでした", lines[0])
        self.assertIn("9600", lines[0], "実際の値が分からない: %r" % lines[0])

    def test_an_accepted_change_is_still_logged(self):
        """対照: 受け付けられた変更は、これまでどおり「設定しました」と残ること。"""
        window = self._window()
        self._attach(window, _PickyPort(9600))

        text = self._set_baudrate(window, 115200)

        self.assertIn("[INFO] COM3 のボーレートを 115200 baud に設定しました", text)
        self.assertNotIn("できませんでした", text)

    def test_a_change_without_a_connection_is_still_logged(self):
        """対照: 接続していないポートの変更も、これまでどおり記録されること。"""
        window = self._window()

        text = self._set_baudrate(window, 115200)

        self.assertIn("[INFO] COM3 のボーレートを 115200 baud に設定しました", text)


if __name__ == "__main__":
    unittest.main()
