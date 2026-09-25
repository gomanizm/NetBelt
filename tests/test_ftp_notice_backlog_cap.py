"""FTP の接続・切断の通知が、GUI への配送待ちとして際限なく積み上がらないこと。

FTP は接続と切断のたびに client_activity を出し、パネルは配送されてから
ログへ足す。ログの 1000 行上限が効くのは配送の後で、その手前の Qt の
配送キューには上限が無かった。GUI が回っている間は積み上がらないが、
他の処理で塞がっている間は、接続して即切断を繰り返すだけ（認証は不要）で
たまり続ける。実測（実パネルを表示し、GUI を 5 秒止めて 4 本で連打）:
17900 件がたまり、はけるまで最初の processEvents が 7.91 秒戻らなかった。

直し方: TFTP の protocol_event、Syslog の _emit_message と同じく、配送待ちの
件数を数えて上限を設ける。超えた分は件数だけ数え、はけた時点で
「N 件の通知を省略しました」を 1 行だけ出す。
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, "src")

CAP = 10
CONNECTIONS = 40


class FtpNoticeBacklogCapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtCore import Qt
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        self.addCleanup(self.m.stop)
        self.emitted = []      # 発行された時点で数える（配送を待たない）
        self.delivered = []    # GUI スレッドへ配送されたもの
        lock = threading.Lock()

        def _count(*args):
            with lock:
                self.emitted.append(args)

        root = tempfile.mkdtemp(prefix="netbelt-ftp-backlog-")
        self.assertTrue(self.m.start(port=0, root_dir=root,
                                     username="u", password="p"))
        self.app.processEvents()   # 起動時の通知を流しておく
        self.m.client_activity.connect(_count, Qt.ConnectionType.DirectConnection)
        self.m.client_activity.connect(lambda ip, msg: self.delivered.append(msg))

    def _connect_and_close(self, count):
        for _ in range(count):
            c = socket.socket()
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00")
            c.settimeout(5)
            c.connect(("127.0.0.1", self.m.port))
            c.close()

    def _wait_handled(self, notices, seconds=10.0):
        """GUI を回さずに、サーバが全接続の接続・切断を処理し終えるのを待つ"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            dropped = getattr(self.m, "_dropped_notices", 0)
            if len(self.emitted) + dropped >= notices:
                return True
            time.sleep(0.05)
        return False

    def _drain(self):
        for _ in range(50):
            before = len(self.delivered)
            self.app.processEvents()
            if len(self.delivered) == before:
                return

    def test_there_is_a_default_cap_on_undelivered_notices(self):
        self.assertTrue(hasattr(self.m, "max_pending_notices"),
                        "接続通知の配送待ちに上限が無い")
        self.assertGreater(self.m.max_pending_notices, 0)

    def test_connection_notices_stop_piling_up_while_the_gui_is_busy(self):
        self.m.max_pending_notices = CAP
        # GUI（このスレッド）は回さないまま、接続と切断を繰り返す
        self._connect_and_close(CONNECTIONS)
        self.assertTrue(self._wait_handled(2 * CONNECTIONS),
                        "前提: サーバが接続を処理しきらない（%d 件）" % len(self.emitted))
        self.assertEqual(len(self.emitted), CAP,
                         "配送待ちが上限で頭打ちにならない（%d 件が配送待ち）"
                         % len(self.emitted))

        self._drain()
        omitted = [m for m in self.delivered if "省略" in m]
        self.assertEqual(len(omitted), 1, "省略の通知が 1 行でない: %r" % omitted)
        self.assertIn(str(2 * CONNECTIONS - CAP), omitted[0])
        self.assertEqual(len(self.delivered), CAP + 1)

        # はけた後は、また通常どおり届く
        self._connect_and_close(1)
        deadline = time.time() + 5
        while len(self.delivered) < CAP + 3 and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertEqual(self.delivered[CAP + 1:], ["接続", "切断"])


if __name__ == "__main__":
    unittest.main()
