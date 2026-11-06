# -*- coding: utf-8 -*-
"""TFTP の待受スレッドが受信の例外で黙って終わらないことを確かめる。

_serve は socket.timeout 以外の例外をすべて break で受けて待受ループを抜けていた。
Windows の UDP は、送った相手のポートが閉じていると ICMP Port Unreachable を受けて
次の recvfrom が WinError 10054（ConnectionResetError）になる。_refuse（server busy）
だけが待受ソケットから送るので、ワーカー枠を埋めたうえで閉じたポートから要求を
1 本送るだけで待受スレッドが終わり、is_running は True のまま、パネルは「起動中」を
出し続けるのに以後どの要求にも応えなくなっていた。

直し方: 10054 などの一過性の受信エラーは読み捨てて待受を続ける。回復できない例外で
待受が終わったときは、マネージャーが停止の扱いにして理由をパネルへ伝える。
"""
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from core.tftp_server import TFTPServer, TFTPServerManager, OP_ERROR  # noqa: E402


def _wrq(filename, mode=b"octet"):
    return b"\x00\x02" + filename.encode() + b"\x00" + mode + b"\x00"


def _rrq(filename, mode=b"octet"):
    return b"\x00\x01" + filename.encode() + b"\x00" + mode + b"\x00"


class _FlakySocket:
    """待受ソケットの代わり。用意した例外を順に recvfrom で投げ、尽きたら本物へ渡す"""

    def __init__(self, real, errors):
        self._real = real
        self._errors = list(errors)
        self._lock = threading.Lock()

    def recvfrom(self, size):
        with self._lock:
            error = self._errors.pop(0) if self._errors else None
        if error is not None:
            raise error
        return self._real.recvfrom(size)

    def __getattr__(self, name):
        return getattr(self._real, name)


class ListenerSurvivesRecvErrorsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-listen-")
        self.srv = TFTPServer(port=0, root_dir=self.root)
        self.srv.start()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        return c

    def _assert_still_answers(self):
        c = self._client()
        c.sendto(_rrq("missing-file.bin"), ("127.0.0.1", self.port))
        try:
            reply, _ = c.recvfrom(1024)
        except socket.timeout:
            self.fail("待受スレッドが終わっていて、要求に応えない")
        self.assertEqual(struct.unpack("!H", reply[:2])[0], OP_ERROR)

    def test_busy_reply_to_a_closed_port_does_not_kill_the_listener(self):
        self.srv.max_workers = 1
        holder = self._client()
        holder.sendto(_wrq("hold.bin"), ("127.0.0.1", self.port))
        holder.recvfrom(1024)            # ACK(0) が来た = 枠が埋まった
        gone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        gone.sendto(_wrq("gone.bin"), ("127.0.0.1", self.port))
        gone.close()                     # server busy の返事は閉じたポートへ向かう
        time.sleep(1.5)
        self.assertTrue(self.srv._thread.is_alive(),
                        "閉じたポートへの返事のあと待受スレッドが終わった")
        self._assert_still_answers()

    def _inject(self, errors):
        self.srv._sock = _FlakySocket(self.srv._sock, errors)
        time.sleep(1.5)                  # 待受が 1 秒の待ちから戻って例外を踏むまで

    def test_connection_reset_on_recv_is_ignored(self):
        self._inject([ConnectionResetError(10054, "reset by peer")])
        self.assertTrue(self.srv._thread.is_alive(), "10054 で待受スレッドが終わった")
        self._assert_still_answers()

    def test_message_too_long_on_recv_is_ignored(self):
        self._inject([OSError(None, "message too long", None, 10040)])
        self.assertTrue(self.srv._thread.is_alive(), "10040 で待受スレッドが終わった")
        self._assert_still_answers()


class FatalListenerErrorStopsManagerTest(unittest.TestCase):
    def test_fatal_recv_error_is_reported_and_stops_the_server(self):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        root = tempfile.mkdtemp(prefix="netbelt-tftp-fatal-")
        m = TFTPServerManager()
        errors, stopped = [], []
        m.error_occurred.connect(errors.append)
        m.stopped.connect(lambda: stopped.append(True))
        self.assertTrue(m.start(port=0, root_dir=root))
        self.addCleanup(m.stop)
        srv = m._srv
        srv._sock = _FlakySocket(srv._sock, [OSError(None, "not a socket", None, 10038)])
        limit = time.monotonic() + 5
        while m.is_running and time.monotonic() < limit:
            app.processEvents()
            time.sleep(0.05)
        app.processEvents()
        self.assertFalse(m.is_running, "待受が終わったのに is_running が True のまま")
        self.assertTrue(stopped, "停止の通知が出ていない（パネルが起動中のまま）")
        self.assertEqual(len(errors), 1)
        self.assertIn("not a socket", errors[0])


class PanelFollowsServerStopTest(unittest.TestCase):
    def test_stopped_signal_restores_the_start_button_and_inputs(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from ui.tftp_server_panel import TFTPServerPanel
        panel = TFTPServerPanel()
        self.addCleanup(panel.deleteLater)
        panel.start_btn.setVisible(False)
        panel.stop_btn.setVisible(True)
        for w in panel._inputs:
            w.setEnabled(False)
        panel.tftp_server.stopped.emit()
        self.assertFalse(panel.start_btn.isHidden(), "起動ボタンが戻らない")
        self.assertTrue(panel.stop_btn.isHidden(), "停止ボタンが残る")
        for w in panel._inputs:
            self.assertTrue(w.isEnabled(), "入力欄が無効のまま: %r" % w)


if __name__ == "__main__":
    unittest.main()
