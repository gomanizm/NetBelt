"""TFTP の protocol_event が、GUI への配送待ちとして際限なく積み上がらないこと。

TFTP の転送ワーカーは、失敗した要求（存在しないファイルへの RRQ など）ごとに
protocol_event を出し、パネルは配送されてからログへ足す。ログの 1000 行上限が
効くのは配送の後で、その手前の Qt の配送キューには上限が無かった。
実測: GUI が回っている間は、サーバの発行（約 2500 件/秒）と GUI の処理
（約 2200 件/秒）がほぼ釣り合って積み上がらなかったが、GUI を 5 秒止めると
15128 件がたまり、はけるまで最初の processEvents が 6.55 秒戻らなかった。
要求は認証なしで送れる。

直し方: Syslog の _emit_message と同じく、配送待ちの件数を数えて上限を設ける。
超えた分は件数だけ数え、はけた時点で「N 件の通知を省略しました」を
client_activity で 1 行だけ出す。
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, "src")

CAP = 10
BURST = 250


class TftpNoticeBacklogCapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtCore import Qt
        from core.tftp_server import TFTPServerManager
        self.m = TFTPServerManager()
        self.emitted = []      # 発行された時点で数える（配送を待たない）
        self.delivered = []    # GUI スレッドへ配送されたもの
        self.activity = []
        lock = threading.Lock()

        def _count(*args):
            with lock:
                self.emitted.append(args)

        self.m.protocol_event.connect(_count, Qt.ConnectionType.DirectConnection)
        self.m.protocol_event.connect(lambda *a: self.delivered.append(a))
        self.m.client_activity.connect(lambda ip, msg: self.activity.append((ip, msg)))

    def _burst_from_worker(self, count):
        """転送ワーカーと同じく、GUI 以外のスレッドから失敗を通知する。"""
        def run():
            for i in range(count):
                self.m._on_event("protocol_error", "192.0.2.10",
                                 ("missing-%d.bin" % i, "ファイルがありません", "download"))
        t = threading.Thread(target=run)
        t.start()
        t.join(10)

    def _drain(self):
        for _ in range(50):
            before = (len(self.delivered), len(self.activity))
            self.app.processEvents()
            if (len(self.delivered), len(self.activity)) == before:
                return

    def test_there_is_a_default_cap_on_undelivered_notices(self):
        self.assertTrue(hasattr(self.m, "max_pending_notices"),
                        "protocol_event の配送待ちに上限が無い")
        self.assertGreater(self.m.max_pending_notices, 0)

    def test_notices_stop_piling_up_while_the_gui_is_busy(self):
        self.m.max_pending_notices = CAP
        # GUI（このスレッド）は回さないまま、上限を大きく超えて通知させる
        self._burst_from_worker(BURST)
        self.assertEqual(len(self.emitted), CAP,
                         "配送待ちが上限で頭打ちにならない（%d 件が配送待ち）"
                         % len(self.emitted))
        self.assertEqual(self.delivered, [])

        self._drain()
        self.assertEqual(len(self.delivered), CAP)
        dropped = [msg for ip, msg in self.activity if "省略" in msg]
        self.assertEqual(len(dropped), 1, "省略の通知が 1 行でない: %r" % self.activity)
        self.assertIn(str(BURST - CAP), dropped[0])

        # はけた後は、また通常どおり届く
        self._burst_from_worker(1)
        self._drain()
        self.assertEqual(len(self.delivered), CAP + 1)
        self.assertEqual(len([m for _, m in self.activity if "省略" in m]), 1)

    def test_nothing_is_reported_as_omitted_when_the_gui_keeps_up(self):
        self.m.max_pending_notices = CAP
        self._burst_from_worker(CAP)
        self._drain()
        self.assertEqual(len(self.delivered), CAP)
        self.assertEqual([m for _, m in self.activity if "省略" in m], [])


if __name__ == "__main__":
    unittest.main()
