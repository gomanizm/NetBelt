"""機器側都合の切断のあと、接続の資源が本当に解放されることを検証する。

_on_connection_closed も _on_connection_error も、self.connections から
del するだけで disconnect() を呼んでいなかった。ポートやソケットを閉じる
のは disconnect() だけなので、機器側が切った場合や読み取りエラーの場合は
掴んだまま残る。しかも各 Connection は parent=MainWindow で作られており
Qt から参照され続けるため、辞書から消しても解放されない。

Windows の COM ポートは同一プロセス内でも排他なので、掴まれたままだと
同じ機器への再接続が必ず「Access is denied」になり、アプリを再起動する
まで復旧できない。タブを閉じても直らない（_on_tab_closed は辞書に残って
いる場合しか disconnect しないので、既に del された後では何もしない）。

ただし後始末から disconnect() をそのまま呼ぶことはできない。末尾の
disconnected.emit() が _on_connection_closed をもう一度呼び、切断通知と
後始末が二重に走る。資源を閉じるだけで通知を出さない口が要る。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DisposeReleasesResourcesTest(unittest.TestCase):
    """各 Connection に「閉じるが通知は出さない」口があること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _watch(self, conn):
        """disconnected が飛んだ回数を数える。"""
        seen = []
        conn.disconnected.connect(lambda: seen.append(1))
        return seen

    # --- シリアル ---

    def _serial(self):
        from core.serial_connection import SerialConnection
        conn = SerialConnection("COM99", 9600)
        port = mock.Mock()
        port.is_open = True
        conn.serial_conn = port
        conn._is_connected = True
        return conn, port

    def test_disposing_a_serial_connection_closes_the_port(self):
        conn, port = self._serial()
        conn.dispose()
        port.close.assert_called_once()
        self.assertIsNone(conn.serial_conn, "ポートへの参照が残っている")

    def test_disposing_a_serial_connection_does_not_announce_a_disconnect(self):
        """通知を出すと、いま処理中の切断がもう一度回ってくる。"""
        conn, _ = self._serial()
        seen = self._watch(conn)
        conn.dispose()
        self.assertEqual(seen, [], "後始末が切断通知を出している")

    def test_disconnecting_a_serial_connection_still_announces_it(self):
        """利用者が切る通常の経路では、これまでどおり通知すること。"""
        conn, port = self._serial()
        seen = self._watch(conn)
        conn.disconnect()
        port.close.assert_called_once()
        self.assertEqual(len(seen), 1, "通常の切断で通知が出ていない")

    # --- SSH ---

    def _ssh(self):
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin")
        conn.client = mock.Mock()
        conn.channel = mock.Mock()
        conn.is_connected = True
        return conn

    def test_disposing_an_ssh_connection_closes_the_client(self):
        conn = self._ssh()
        client = conn.client
        conn.dispose()
        client.close.assert_called_once()
        self.assertIsNone(conn.client, "SSHClient への参照が残っている")

    def test_disposing_an_ssh_connection_does_not_announce_a_disconnect(self):
        conn = self._ssh()
        seen = self._watch(conn)
        conn.dispose()
        self.assertEqual(seen, [], "後始末が切断通知を出している")

    # --- Telnet ---

    def _telnet(self):
        from core.telnet_connection import TelnetConnection
        conn = TelnetConnection("192.0.2.1", 23)
        conn.socket = mock.Mock()
        conn.is_connected = True
        return conn

    def test_disposing_a_telnet_connection_closes_the_socket(self):
        conn = self._telnet()
        sock = conn.socket
        conn.dispose()
        sock.close.assert_called_once()
        self.assertIsNone(conn.socket, "ソケットへの参照が残っている")

    def test_disposing_a_telnet_connection_does_not_announce_a_disconnect(self):
        conn = self._telnet()
        seen = self._watch(conn)
        conn.dispose()
        self.assertEqual(seen, [], "後始末が切断通知を出している")


class MainWindowReleasesConnectionsTest(unittest.TestCase):
    """切断・エラーを受けたら、接続を閉じてから捨てること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-dispose-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            return MainWindow()

    def test_a_read_error_releases_the_connection(self):
        """読み取りエラーで、掴んでいるポートを手放すこと。"""
        w = self._window()
        conn = mock.Mock()
        w.connections["ルータA"] = conn

        w._on_connection_error("ルータA", "読み取りエラー: アクセスが拒否されました")

        conn.dispose.assert_called_once()
        self.assertNotIn("ルータA", w.connections)

    def test_the_device_closing_the_session_releases_the_connection(self):
        """機器側が切った場合も手放すこと。"""
        w = self._window()
        conn = mock.Mock()
        w.connections["ルータA"] = conn

        w._on_connection_closed("ルータA")

        conn.dispose.assert_called_once()
        self.assertNotIn("ルータA", w.connections)

    def test_releasing_does_not_run_the_close_handling_twice(self):
        """後始末が切断処理を再入させないこと。

        disconnect() を呼ぶと末尾の disconnected.emit() が
        _on_connection_closed をもう一度呼び、案内が2回出る。
        """
        from core.serial_connection import SerialConnection
        w = self._window()
        conn = SerialConnection("COM99", 9600, w)
        port = mock.Mock()
        port.is_open = True
        conn.serial_conn = port
        conn._is_connected = True
        conn.disconnected.connect(
            lambda: w._on_connection_closed("ルータA"))
        w.connections["ルータA"] = conn

        with mock.patch.object(w.terminal_widget, "show_notice") as notice:
            w._on_connection_closed("ルータA")

        self.assertEqual(notice.call_count, 1,
                         "切断の案内が二重に出ている")
        port.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
