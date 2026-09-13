"""後始末した接続と SFTP マネージャが、本当に手放されることを検証する。

各 Connection と SFTPManager は parent=MainWindow で作られる。閉じて辞書
から外しても親子関係はそのままなので、Qt が参照を持ち続け、接続・切断を
繰り返すほど抜け殻が積み上がる。実測では 300 サイクルで SSHConnection と
SFTPManager が各 305 個、RSS が 80.3MB から 89.2MB（1 サイクル約 30KB）。
長く使うほど増える一方で、閉じても減らない。

資源（ソケット・COM ポート）は dispose() が閉じているので危険は無いが、
抜け殻が残らないようにする。破棄は deleteLater に任せる。切断処理は
接続自身の disconnected シグナルの中から呼ばれるので、その場で
破棄すると発行中のオブジェクトを壊すことになる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DisposedObjectsAreReleasedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.windows = []       # ウィンドウを先に捨てると子ごと消えるので保持する

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-release-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self).windows.append(w)
        return w

    @staticmethod
    def _flush_deferred_deletes():
        from PyQt6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _serial(self, w):
        """接続済みに見せかけたシリアル接続を登録する。"""
        from core.serial_connection import SerialConnection
        conn = SerialConnection("COM99", 9600, w)
        port = mock.Mock()
        port.is_open = True
        conn.serial_conn = port
        conn._is_connected = True
        return conn

    def _count(self, w):
        from core.serial_connection import SerialConnection
        return len(w.findChildren(SerialConnection))

    def test_a_closed_session_is_no_longer_a_child_of_the_window(self):
        w = self._window()
        conn = self._serial(w)
        w.connections["ルータA"] = conn

        w._on_connection_closed("ルータA", conn)
        self._flush_deferred_deletes()

        self.assertEqual(self._count(w), 0,
                         "閉じた接続がウィンドウの子として残っている")

    def test_a_connection_error_releases_the_object_too(self):
        w = self._window()
        conn = self._serial(w)
        w.connections["ルータA"] = conn

        w._on_connection_error("ルータA", "読み取りエラー", conn)
        self._flush_deferred_deletes()

        self.assertEqual(self._count(w), 0,
                         "エラーで捨てた接続がウィンドウの子として残っている")

    def test_a_replaced_session_is_released(self):
        """置き換え済みの古い接続からの通知でも、抜け殻を残さないこと。"""
        w = self._window()
        current = self._serial(w)
        stale = self._serial(w)
        w.connections["ルータA"] = current

        w._on_connection_closed("ルータA", stale)
        self._flush_deferred_deletes()

        self.assertEqual(self._count(w), 1, "現行の接続まで捨てている")
        self.assertIs(w.connections["ルータA"], current)

    def test_repeated_connect_and_disconnect_does_not_pile_up(self):
        w = self._window()

        for _ in range(10):
            conn = self._serial(w)
            w.connections["ルータA"] = conn
            w._on_connection_closed("ルータA", conn)
            self._flush_deferred_deletes()

        self.assertEqual(self._count(w), 0,
                         "切断のたびに抜け殻が積み上がっている")

    def test_a_dropped_sftp_manager_is_released(self):
        from core.sftp_manager import SFTPManager
        w = self._window()
        mgr = SFTPManager(w)
        w.sftp_managers["ルータA"] = mgr

        w._drop_sftp_manager("ルータA")
        self._flush_deferred_deletes()

        self.assertEqual(len(w.findChildren(SFTPManager)), 0,
                         "外した SFTP マネージャがウィンドウの子として残っている")


    def _sftp_count(self, w):
        from core.sftp_manager import SFTPManager
        return len(w.findChildren(SFTPManager))

    def test_an_unsupported_sftp_session_is_released(self):
        """SFTP 非対応の機器へ繋いでも、抜け殻を残さないこと。

        SFTPManager は親を MainWindow にして作られ、成功したときだけ
        sftp_managers に入る。ip scp/sftp を有効にしていない機器では
        毎回 ok=False で返るので、繋ぐたびに 1 個ずつ積み上がる。
        """
        from core.sftp_manager import SFTPManager
        w = self._window()
        conn = self._serial(w)
        w.connections["ルータA"] = conn

        for _ in range(10):
            w._on_sftp_session_ready(
                "ルータA", SFTPManager(w), conn, False)
            self._flush_deferred_deletes()

        self.assertEqual(self._sftp_count(w), 0,
                         "SFTP 非対応の機器へ繋ぐたびに抜け殻が積み上がっている")
        self.assertEqual(w.sftp_managers, {})

    def test_a_stale_sftp_session_is_released(self):
        """待っている間に繋ぎ直したときも、抜け殻を残さないこと。"""
        from core.sftp_manager import SFTPManager
        w = self._window()
        current = self._serial(w)
        stale = self._serial(w)
        w.connections["ルータA"] = current

        for _ in range(10):
            w._on_sftp_session_ready(
                "ルータA", SFTPManager(w), stale, True)
            self._flush_deferred_deletes()

        self.assertEqual(self._sftp_count(w), 0,
                         "古い SFTP セッションの抜け殻が積み上がっている")
        self.assertEqual(w.sftp_managers, {})

    def test_a_live_sftp_session_is_kept(self):
        """成功した SFTP セッションまで捨てないこと。"""
        from core.sftp_manager import SFTPManager
        w = self._window()
        conn = self._serial(w)
        w.connections["ルータA"] = conn
        mgr = SFTPManager(w)

        w._on_sftp_session_ready("ルータA", mgr, conn, True)
        self._flush_deferred_deletes()

        self.assertIs(w.sftp_managers["ルータA"], mgr)
        self.assertEqual(self._sftp_count(w), 1)

if __name__ == "__main__":
    unittest.main()
