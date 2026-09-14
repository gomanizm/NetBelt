"""置き換え済みの接続が出す SFTP エラーがステータスバーを汚さないことを検証する。

_start_sftp_session が張るラムダは機器名しか束縛していないので、同名で
繋ぎ直した直後に旧セッションの open_sftp が失敗すると、_on_sftp_error が
そのまま走り、成功した新接続の「接続しました」を SFTP エラーで上書きする。
遅れて届く _sftp_session_ready の側は接続の同一性を見て捨てているのに、
error_occurred の側だけが素通りする。

現在の接続が出すエラーは、これまでどおりステータスバーに出す。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpErrorFromReplacedConnectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-sftperr-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self)._windows.append(w)
        return w

    def _spy_on_sftp_error(self, w):
        """_on_sftp_error の呼び出しを記録する。本体はそのまま走らせる。"""
        calls = []
        original = w._on_sftp_error

        # 呼び出し側が渡す引数の数は問わない（束縛が増えても記録できる）
        def spy(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        w._on_sftp_error = spy
        return calls

    def _pump_until(self, predicate, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        self.app.processEvents()
        return predicate()

    def _conn_failing_sftp(self, w, gate):
        """open_sftp が gate を待ってから失敗する、接続済みの SSHConnection"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin", parent=w)
        conn.is_connected = True

        def open_sftp():
            gate.wait(5)
            raise OSError("subsystem request failed")

        conn.client = mock.Mock()
        conn.client.open_sftp.side_effect = open_sftp
        return conn

    def test_error_from_replaced_connection_leaves_status_bar_alone(self):
        """旧接続の SFTP エラーは、新接続のステータス表示を上書きしないこと。"""
        w = self._window()
        gate = threading.Event()
        old = self._conn_failing_sftp(w, gate)
        w.connections["R1"] = old
        calls = self._spy_on_sftp_error(w)

        w._start_sftp_session("R1", old)

        # 待っている間に同名で繋ぎ直し、新接続の成功表示を出す
        new = mock.Mock()
        w.connections["R1"] = new
        w.status_bar.showMessage("R1 に接続しました")

        gate.set()
        self.assertTrue(
            self._pump_until(lambda: calls, 5),
            "旧接続の SFTP エラーが届いていない（テストが穴を素通りしている）")
        self.assertEqual(w.status_bar.currentMessage(), "R1 に接続しました",
                         "置き換え済みの接続のエラーでステータスバーが上書きされた")

    def test_error_from_the_current_connection_is_shown(self):
        """現在の接続の SFTP エラーは、これまでどおり表示すること。"""
        w = self._window()
        gate = threading.Event()
        gate.set()
        conn = self._conn_failing_sftp(w, gate)
        w.connections["R1"] = conn
        calls = self._spy_on_sftp_error(w)

        w._start_sftp_session("R1", conn)

        self.assertTrue(
            self._pump_until(lambda: calls, 5),
            "SFTP エラーが届いていない")
        self.assertIn("SFTP エラー (R1)", w.status_bar.currentMessage())


if __name__ == "__main__":
    unittest.main()
