# -*- coding: utf-8 -*-
"""受信スレッドが想定外の例外で終わっても、「受信中」のまま残らないこと。

UDP の受信ループ・TCP の受付ループは、例外の枝の中の print が例外を出すと
（exe でログの保存先が満杯のときなど）ループを抜けてスレッドが終わる。
それでも登録（_servers）が残っていたので、is_running は True のまま、パネルは
「受信中」と表示し続け、以後は 1 件も受け付けなかった。
"""
import builtins
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _failing_print_off(main_thread):
    """main_thread 以外のスレッドでだけ print が OSError を出す print を返す"""
    real_print = builtins.print

    def fake(*args, **kwargs):
        if threading.current_thread() is not main_thread:
            raise OSError(28, "No space left on device")
        return real_print(*args, **kwargs)
    return fake


class SyslogLoopCrashStateTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()
        cls._keep.clear()

    def setUp(self):
        from core.syslog_receiver import SyslogReceiver
        self.recv = SyslogReceiver()
        self._keep.append(self.recv)
        self.errors = []
        self.stopped = []
        self.recv.error_occurred.connect(self.errors.append)
        self.recv.stopped.connect(lambda: self.stopped.append(True))
        self.addCleanup(self.recv.stop)

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def _connect(self, port, payload=None):
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(c.close)
        if payload:
            c.sendall(payload)

    def _crash_with_failing_print(self, proto, poke):
        """受信側のスレッドで print を失敗させ、poke で例外を起こさせる"""
        if not self.recv.is_protocol_running(proto):
            self.assertTrue(self.recv.start_protocol(proto, 0))
        port = self.recv.active_port(proto)
        thread = self.recv._servers[proto]["thread"]
        with mock.patch("builtins.print",
                        _failing_print_off(threading.current_thread())):
            poke(port)
            self.assertTrue(self._wait(lambda: not thread.is_alive()),
                            "受信スレッドが例外で終わる前提が崩れた")

    def _assert_reported_stopped(self, proto):
        self.assertTrue(self._wait(lambda: not self.recv.is_protocol_running(proto)),
                        "スレッドが終わったのに %s が稼働中のまま" % proto)
        self.assertFalse(self.recv.is_running)
        self.assertTrue(self._wait(lambda: self.errors), "理由が通知されない")
        self.assertIn(proto, self.errors[-1])
        self.assertTrue(self._wait(lambda: self.stopped), "stopped が出ない")

    def test_tcp_accept_loop_death_clears_the_running_state(self):
        self._crash_with_failing_print(
            "TCP", lambda port: self._connect(port, b"<14>first\n"))
        self._assert_reported_stopped("TCP")

    def test_udp_loop_death_clears_the_running_state(self):
        from core import syslog_receiver as mod

        def broken(*_a, **_k):
            raise ValueError("unexpected")

        def poke(port):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(s.close)
            s.sendto(b"<14>x", ("127.0.0.1", port))
        with mock.patch.object(mod, "SyslogMessage", broken):
            self._crash_with_failing_print("UDP", poke)
        self._assert_reported_stopped("UDP")

    def test_the_other_protocol_keeps_running(self):
        self.assertTrue(self.recv.start_protocol("UDP", 0))
        self._crash_with_failing_print("TCP", self._connect)
        self.assertTrue(self._wait(lambda: not self.recv.is_protocol_running("TCP")))
        self.assertTrue(self.recv.is_protocol_running("UDP"))
        self.app.processEvents()
        self.assertFalse(self.stopped)

    def test_restart_after_death_works(self):
        self._crash_with_failing_print("TCP", self._connect)
        self.assertTrue(self._wait(lambda: not self.recv.is_running))
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        self.assertTrue(self.recv.is_protocol_running("TCP"))

    def test_panel_shows_stopped_after_the_thread_dies(self):
        from ui.syslog_panel import SyslogPanel
        panel = SyslogPanel(config_manager=None)
        self._keep.append(panel)
        panel.set_syslog_receiver(self.recv)
        with mock.patch("ui.syslog_panel.QMessageBox.critical") as critical:
            # パネルの「受信開始」と同じ状態にする（ポートは空きを使う）
            self.assertTrue(self.recv.start_protocol("TCP", 0))
            panel.start_btn.setVisible(False)
            panel.stop_btn.setVisible(True)
            panel.pause_action.setEnabled(True)
            panel._set_ports_enabled(False)
            panel._update_recv_status()
            self.assertIn("受信中", panel.recv_status_label.text())
            self._crash_with_failing_print("TCP", self._connect)
            self.assertTrue(self._wait(lambda: critical.called), "理由が表示されない")
            self.assertTrue(self._wait(
                lambda: "停止中" in panel.recv_status_label.text()),
                "パネルが受信中のまま: %s" % panel.recv_status_label.text())
            self.assertFalse(panel.start_btn.isHidden())
            self.assertTrue(panel.stop_btn.isHidden())
            self.assertTrue(panel.udp_port_spin.isEnabled())


if __name__ == "__main__":
    unittest.main()
