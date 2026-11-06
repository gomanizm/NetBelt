# -*- coding: utf-8 -*-
"""FTP の待受スレッドが想定外の例外で止まったとき、停止として扱うことを検証する。

_serve は ioloop.loop() を例外から守っておらず、poll が例外を投げると
（Windows の select は 512 本を超える fd で ValueError）待受スレッドが終わり、
待受ソケットも閉じるのに is_running は True のまま、パネルも「起動中」を
出し続けていた（新しい接続はすべて拒否される）。理由も知らされなかった。
"""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")


def _wait(app, cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    app.processEvents()
    return cond()


class FtpListenerCrashTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patch = unittest.mock.patch(
            "core.firewall.ensure_inbound_allow", return_value=(True, "test stub"))
        patch.start()
        self.addCleanup(patch.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-crash-")

    def _break_poll_once(self, manager):
        ioloop = manager._server.ioloop
        orig = ioloop.poll
        state = {"fired": False}

        def poll(timeout):
            if not state["fired"]:
                state["fired"] = True
                raise ValueError("too many file descriptors in select()")
            return orig(timeout)
        ioloop.poll = poll

    def _manager(self):
        from core.ftp_server import FTPServerManager
        m = FTPServerManager()
        self.addCleanup(m.stop)
        return m

    def test_manager_reports_the_crash_and_is_no_longer_running(self):
        m = self._manager()
        errors, stopped = [], []
        m.error_occurred.connect(errors.append)
        m.stopped.connect(lambda: stopped.append(True))
        self.assertTrue(m.start(port=0, root_dir=self.root, anonymous=True))
        thread = m._thread
        self._break_poll_once(m)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(_wait(self.app, lambda: not m.is_running),
                        "待受スレッドが止まったのに is_running が True のまま")
        self.assertTrue(_wait(self.app, lambda: errors and stopped))
        self.assertTrue(any("too many file descriptors" in e for e in errors), errors)

    def test_server_can_be_started_again_after_the_crash(self):
        m = self._manager()
        self.assertTrue(m.start(port=0, root_dir=self.root, anonymous=True))
        self._break_poll_once(m)
        self.assertTrue(_wait(self.app, lambda: not m.is_running))
        self.assertTrue(m.start(port=0, root_dir=self.root, anonymous=True))
        self.assertTrue(m.is_running)
        self.assertTrue(m._thread.is_alive())

    def test_panel_returns_to_the_stopped_state(self):
        import socket
        from core.config_manager import ConfigManager
        from ui.ftp_server_panel import FTPServerPanel
        d = tempfile.mkdtemp(prefix="netbelt-ftp-crash-panel-")
        panel = FTPServerPanel(config_manager=ConfigManager(
            config_path=os.path.join(d, "config.json")))
        self.addCleanup(panel.ftp_server.stop)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        panel.port_spin.setValue(port)
        panel.root_dir_edit.setText(self.root)
        panel.anonymous_check.setChecked(True)
        with unittest.mock.patch("ui.ftp_server_panel.QMessageBox.critical") as critical:
            panel._on_start_server()
            self.assertTrue(panel.ftp_server.is_running)
            self.assertTrue(panel.start_btn.isHidden())
            self._break_poll_once(panel.ftp_server)
            self.assertTrue(_wait(self.app, lambda: critical.called))
        self.assertTrue(_wait(self.app, lambda: not panel.start_btn.isHidden()),
                        "待受が止まったのに起動ボタンに戻らない")
        self.assertTrue(panel.stop_btn.isHidden())
        self.assertTrue(panel.port_spin.isEnabled())
        self.assertIn("停止中", panel.status_label.text())


if __name__ == "__main__":
    unittest.main()
