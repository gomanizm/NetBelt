"""起動直後の SFTP サーバーも、ウィンドウを閉じたら止めること。

SFTPServer.is_running を True にするのは待受ワーカーの先頭で、start() は
スレッドを起こした直後に戻る。その隙に閉じると closeEvent の
`if ... .sftp_server.is_running:` が偽になり stop() を呼ばない。実測では
閉じたあとも待受が生き続け、2 秒後に "Server started on port ..." を出して
127.0.0.1 から接続を受け付け、ハンドラまで動いた。

SFTPServer.stop() 自身は同じ理由で is_running を見ず、待受スレッドの
生死で判定している。closeEvent も同じ基準にする。

TFTP と FTP は start() の中で（呼び出し元のスレッドで）is_running を
立ててから戻るので、この隙は無い。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _FakeSFTPServer:
    """start() 直後（ワーカーがまだ is_running を立てていない）サーバー。"""

    def __init__(self, alive):
        self.is_running = False
        self.stop_calls = 0
        self.server_thread = None
        self._release = threading.Event()
        if alive:
            self.server_thread = threading.Thread(
                target=self._release.wait, args=(5,), daemon=True)
            self.server_thread.start()
            for _ in range(100):          # スレッドが走り出すまで待つ
                if self.server_thread.is_alive():
                    break
                time.sleep(0.01)

    def stop(self):
        self.stop_calls += 1
        self._release.set()


class SFTPServerStopOnCloseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.windows = []       # ウィンドウを先に捨てると子ごと消えるので保持する

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-close-sftpd-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self).windows.append(w)
        return w

    def _close(self, w):
        from PyQt6.QtGui import QCloseEvent
        w.closeEvent(QCloseEvent())

    def test_closing_stops_a_server_whose_worker_has_not_started_yet(self):
        w = self._window()
        server = _FakeSFTPServer(alive=True)
        w.sftp_server_panel.sftp_server = server
        self.assertFalse(server.is_running, "前提: フラグはまだ立っていない")

        self._close(w)

        self.assertEqual(server.stop_calls, 1,
                         "起動直後の SFTP サーバーが止められていない")

    def test_closing_does_not_touch_a_server_that_was_never_started(self):
        w = self._window()
        server = _FakeSFTPServer(alive=False)
        w.sftp_server_panel.sftp_server = server

        self._close(w)

        self.assertEqual(server.stop_calls, 0)

    def test_closing_still_stops_a_running_server(self):
        w = self._window()
        server = _FakeSFTPServer(alive=True)
        server.is_running = True
        w.sftp_server_panel.sftp_server = server

        self._close(w)

        self.assertEqual(server.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
