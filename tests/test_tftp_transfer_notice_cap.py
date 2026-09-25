"""TFTP の転送の通知（開始・進捗・完了・中断）も、GUI への配送待ちの上限に従うこと。

配送待ちの上限（max_pending_notices）が効いていたのは protocol_event だけで、
transfer_started / transfer_progress / transfer_complete / transfer_interrupted は
対象外だった。存在する小さいファイルへの RRQ に即 ACK を返すのは認証なしで
できるので、GUI が塞がっている間に連打されると、Qt の配送キューへ際限なく
積み上がる。実測（実パネル、GUI を 5 秒止めて 4 スレッドから RRQ→ACK を連打）:
その 5 秒で started 8137 件・progress 10521 件・complete 10521 件が発行され、
再開後の最初の processEvents が 12.53 秒戻らなかった。

直し方: 転送の通知も protocol_event と同じ配送待ちのカウンタに数える。
上限に達している間は、進捗は捨てる（次の進捗か完了で置き換わる）。開始を
捨てた転送は、その進捗・完了・中断も出さずに省略件数へ足す（パネルの行が
完了だけ・開始だけの片側にならないように）。開始を届けた転送の行を閉じる
最初の完了・中断・失敗（protocol_event）は、上限を超えていても渡す（捨てると
「転送中」のまま残る）。修正後の同じ実測では、止めていた 5 秒の発行は
1001 件（上限 1000）、最初の processEvents は 0.03 秒で戻った。
"""
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, "src")

CAP = 10
BURST = 250
IP = "192.0.2.10"


class TftpTransferNoticeCapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtCore import Qt
        from core.tftp_server import TFTPServerManager
        self.m = TFTPServerManager()
        self.m.max_pending_notices = CAP
        self.emitted = []      # 発行された時点で数える（配送を待たない）
        self.delivered = []    # GUI スレッドへ配送されたもの
        self.activity = []
        lock = threading.Lock()
        direct = Qt.ConnectionType.DirectConnection

        def watch(kind):
            def on_emit(*args):
                with lock:
                    self.emitted.append((kind, args[1]))
            getattr(self.m, "transfer_" + kind).connect(on_emit, direct)
            getattr(self.m, "transfer_" + kind).connect(
                lambda *args: self.delivered.append((kind, args[1])))

        for kind in ("started", "progress", "complete", "interrupted"):
            watch(kind)
        self.failures = []
        self.m.protocol_event.connect(
            lambda ip, fn, reason, d: self.failures.append(fn), direct)
        self.m.client_activity.connect(lambda ip, msg: self.activity.append(msg))

    def _from_worker(self, events):
        """転送ワーカーと同じく、GUI 以外のスレッドから通知する。"""
        def run():
            for kind, payload in events:
                self.m._on_event(kind, IP, payload)
        t = threading.Thread(target=run)
        t.start()
        t.join(10)

    def _drain(self):
        for _ in range(50):
            before = (len(self.delivered), len(self.activity))
            self.app.processEvents()
            if (len(self.delivered), len(self.activity)) == before:
                return

    def _names(self, kind, source):
        return [fn for k, fn in source if k == kind]

    @staticmethod
    def _transfer(name, last="transfer_complete"):
        if last == "interrupted":
            end = ("interrupted", (name, "download"))
        else:
            end = ("transfer_complete", (name, 1, 1, "download"))
        return [("transfer_started", (name, 1, "download")),
                ("transfer_progress", (name, 1, 1, "download")),
                end]

    def test_transfer_notices_stop_piling_up_while_the_gui_is_busy(self):
        events = []
        for i in range(BURST):
            events += self._transfer("f-%d.bin" % i,
                                     "interrupted" if i % 5 == 4 else "complete")
        self._from_worker(events)
        self.assertLessEqual(len(self.emitted), CAP + 1,
                             "転送の通知が上限で頭打ちにならない（%d 件が配送待ち）"
                             % len(self.emitted))
        # 開始を届けた転送は、その行を閉じる通知も届く。開始を省いた転送の
        # 完了・中断は出さない（パネルの行が片側だけにならない）
        started = self._names("started", self.emitted)
        closed = (self._names("complete", self.emitted)
                  + self._names("interrupted", self.emitted))
        self.assertTrue(started)
        self.assertEqual(sorted(started), sorted(closed))

        self._drain()
        self.assertEqual(sorted(self._names("started", self.delivered)), sorted(started))
        omitted = [m for m in self.activity if "省略" in m]
        self.assertEqual(len(omitted), 1, "省略の通知が 1 行でない: %r" % self.activity)

        # はけた後は、また通常どおり届く
        before = len(self.delivered)
        self._from_worker(self._transfer("after.bin"))
        self._drain()
        self.assertEqual([k for k, fn in self.delivered[before:]],
                         ["started", "progress", "complete"])

    def test_a_failure_that_closes_a_shown_row_is_not_dropped(self):
        """開始を届けた行を「エラー」で閉じる通知も、上限を超えて渡すこと。

        転送の通知を同じカウンタに数えるので、成功した転送の通知で上限に
        達している最中の本物の失敗が捨てられると、その行が残り続ける。
        """
        events = [("transfer_started", ("x.bin", 100, "upload"))]
        events += [("transfer_started", ("other-%d.bin" % i, 1, "upload"))
                   for i in range(2 * CAP)]
        events.append(("protocol_error",
                       ("x.bin", "アップロード失敗（保存できません）: disk full", "upload")))
        self._from_worker(events)
        self.assertIn("x.bin", self.failures,
                      "開始を届けた行を閉じる失敗の通知が捨てられた")
        self.assertEqual(self.failures, ["x.bin"])

    def test_nothing_is_omitted_when_the_gui_keeps_up(self):
        for i in range(CAP):
            self._from_worker(self._transfer("ok-%d.bin" % i))
            self._drain()
        self.assertEqual(len(self._names("started", self.delivered)), CAP)
        self.assertEqual(len(self._names("complete", self.delivered)), CAP)
        self.assertEqual([m for m in self.activity if "省略" in m], [])


class TftpTransferFloodPanelTest(unittest.TestCase):
    """実際の RRQ→即 ACK の連打でも、配送待ちが上限で止まり、行が片側にならないこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtCore import Qt
        from core.config_manager import ConfigManager
        from ui.tftp_server_panel import TFTPServerPanel
        work = tempfile.mkdtemp(prefix="netbelt-tftp-transfer-cap-")
        self.panel = TFTPServerPanel(
            config_manager=ConfigManager(os.path.join(work, "config.json")))
        self.m = self.panel.tftp_server
        self.m.max_pending_notices = 20
        root = os.path.join(work, "root")
        os.makedirs(root)
        with open(os.path.join(root, "a.txt"), "wb") as f:
            f.write(b"x")
        self.assertTrue(self.m.start(port=0, root_dir=root))
        self.addCleanup(self.m.stop)
        self.app.processEvents()
        self.emitted = 0
        lock = threading.Lock()

        def count(*_args):
            with lock:
                self.emitted += 1

        for sig in (self.m.transfer_started, self.m.transfer_progress,
                    self.m.transfer_complete, self.m.transfer_interrupted):
            sig.connect(count, Qt.ConnectionType.DirectConnection)

    def _rrq_and_ack(self, n):
        port = self.m._srv.port
        for _ in range(n):
            c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            c.settimeout(2)
            try:
                c.sendto(b"\x00\x01a.txt\x00octet\x00", ("127.0.0.1", port))
                data, peer = c.recvfrom(1024)
                if data[:2] == b"\x00\x03":
                    c.sendto(b"\x00\x04" + data[2:4], peer)
            finally:
                c.close()

    def test_a_flood_of_downloads_is_capped_and_rows_are_closed(self):
        # GUI（このスレッド）は回さないまま連打する
        t = threading.Thread(target=self._rrq_and_ack, args=(200,))
        t.start()
        t.join(60)
        time.sleep(0.5)
        self.assertLessEqual(self.emitted, 30,
                             "GUI が止まっている間に転送の通知が %d 件積み上がった"
                             % self.emitted)

        for _ in range(50):
            self.app.processEvents()
        h = self.panel.history
        statuses = [h.item(r, 5).text() for r in range(h.rowCount())]
        self.assertTrue(statuses)
        self.assertEqual([s for s in statuses if s != "完了"], [],
                         "閉じられないまま残った行がある: %r" % statuses)
        self.assertEqual(self.panel._active, {})


if __name__ == "__main__":
    unittest.main()
