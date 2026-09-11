"""SSH 接続直後の SFTP セッション確立が GUI スレッドを止めないことを検証する。

SSHConnection.connected は接続スレッドから emit されるが、受け手はラムダ
なので GUI スレッドへキュー配送され、_on_connection_success は
MainThread で走る。そこで SFTPManager.connect（open_sftp / normalize）を
同期で待つと、機器が subsystem 要求に答えるまでイベントループが完全に
止まる。paramiko の読み取りには timeout が無いので、応答しない機器では
長時間固まりうる。

計測: open_sftp に 2 秒かかる client で、その間の 50ms タイマーは 0 回
しか発火せず（最大間隔 2.062 秒）、open_sftp / normalize はどちらも
MainThread で実行されていた。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpConnectOffGuiThreadTest(unittest.TestCase):
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
        # SFTP パネルの警告はモーダル。万一出ても止まらないようにする
        from ui import sftp_panel as mod
        patcher = mock.patch.object(mod.QMessageBox, "warning")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-sftpconn-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self)._windows.append(w)
        return w

    def _pump_until(self, predicate, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        self.app.processEvents()
        return predicate()

    def _ssh_with_slow_sftp(self, w, gate, record):
        """open_sftp が gate を待つ client を持った、接続済みの SSHConnection"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin", parent=w)
        conn.is_connected = True
        sftp_client = mock.Mock()
        sftp_client.normalize.return_value = "/home/admin"
        # 登録直後にパネルが一覧を取りに来る。Mock を返すと一覧スレッドが
        # 失敗し、パネルがモーダルの警告を出して offscreen では止まる
        sftp_client.listdir_attr.return_value = []

        def open_sftp():
            record["open_sftp_thread"] = threading.current_thread().name
            gate.wait(5)
            return sftp_client

        conn.client = mock.Mock()
        conn.client.open_sftp.side_effect = open_sftp
        return conn, sftp_client

    def test_connection_success_does_not_wait_for_open_sftp(self):
        """open_sftp が返らなくても、接続成功の処理はすぐ戻ること。"""
        w = self._window()
        terminal = w.terminal_widget.create_terminal_tab("R1")
        gate = threading.Event()
        record = {}
        conn, _ = self._ssh_with_slow_sftp(w, gate, record)
        w.connections["R1"] = conn

        started = time.monotonic()
        w._on_connection_success("R1", terminal, conn)
        elapsed = time.monotonic() - started

        gate.set()
        self.assertLess(elapsed, 0.5,
                        "open_sftp の完了を GUI スレッドで待っている")
        self.assertTrue(
            self._pump_until(lambda: "R1" in w.sftp_managers, 3),
            "SFTP マネージャが登録されない")
        self.assertNotEqual(record.get("open_sftp_thread"), "MainThread",
                            "open_sftp が GUI スレッドで実行された")
        # 表示中のタブなので、パネルにも出ること
        self.assertEqual(w.sftp_panel.current_device, "R1")
        self.assertEqual(w.sftp_managers["R1"].current_path, "/home/admin")

    def test_sftp_ready_after_the_tab_was_closed_is_discarded(self):
        """待っている間にタブを閉じたら、遅れて開いた SFTP は登録せず閉じること。"""
        w = self._window()
        terminal = w.terminal_widget.create_terminal_tab("R1")
        gate = threading.Event()
        record = {}
        conn, sftp_client = self._ssh_with_slow_sftp(w, gate, record)
        w.connections["R1"] = conn

        w._on_connection_success("R1", terminal, conn)
        # SFTP が開く前にタブを閉じる
        w._on_tab_closed("R1")
        self.assertNotIn("R1", w.connections)

        gate.set()
        self.assertTrue(
            self._pump_until(lambda: sftp_client.close.called, 3),
            "遅れて開いた SFTP セッションが閉じられていない")
        self.assertNotIn("R1", w.sftp_managers)
        self.assertFalse(w.sftp_panel.current_device)

    def test_sftp_ready_from_a_replaced_connection_is_discarded(self):
        """待っている間に同名で繋ぎ直したら、旧接続の SFTP は登録しないこと。"""
        w = self._window()
        terminal = w.terminal_widget.create_terminal_tab("R1")
        gate = threading.Event()
        record = {}
        old, sftp_client = self._ssh_with_slow_sftp(w, gate, record)
        w.connections["R1"] = old

        w._on_connection_success("R1", terminal, old)
        new = mock.Mock()
        w.connections["R1"] = new

        gate.set()
        self.assertTrue(
            self._pump_until(lambda: sftp_client.close.called, 3),
            "旧接続の SFTP セッションが閉じられていない")
        self.assertNotIn("R1", w.sftp_managers)
        self.assertIs(w.connections["R1"], new)


if __name__ == "__main__":
    unittest.main()
