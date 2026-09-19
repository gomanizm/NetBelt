"""FTP の転送の通知（開始・進捗・完了・中断）も、GUI への配送待ちの上限に従うこと。

FTPServerManager が max_pending_notices に数えていたのは client_activity
（接続・切断）だけで、transfer_started / transfer_progress /
transfer_complete / transfer_interrupted は対象外だった。TFTP で塞いだのと
同じ穴が残っていたことになる。パネルのログと履歴の行数上限が効くのは
配送の後なので、GUI が他の処理で塞がっている間は Qt の配送キューへ
際限なく積み上がる。実測（実パネル、GUI を 5 秒止めて 3 スレッドから
RETR を連打）: 転送の通知 10002 件が積まれる一方、client_activity は
上限どおり 408 件で止まっていた。

直し方: TFTP と同じ形で、転送の通知も同じ配送待ちのカウンタに数える。
上限に達している間、進捗は捨てる（次の進捗か完了で置き換わる）。開始を
捨てた転送は、その進捗・完了・中断も出さずに省略件数へ足す（パネルの行が
完了だけ・開始だけの片側にならないように）。開始を届けた転送の行を閉じる
完了・中断は、上限を超えていても渡す（捨てると「転送中」のまま残る）。
"""
import ftplib
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, "src")

CAP = 10
BURST = 250
IP = "192.0.2.10"


class FtpTransferNoticeCapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtCore import Qt
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
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
            signal = getattr(self.m, "transfer_" + kind)
            signal.connect(on_emit, direct)
            signal.connect(lambda *args: self.delivered.append((kind, args[1])))

        for kind in ("started", "progress", "complete", "interrupted"):
            watch(kind)
        self.m.client_activity.connect(lambda ip, msg: self.activity.append(msg))

    def _from_worker(self, transfers):
        """転送の通知と同じく、GUI 以外のスレッド（待受スレッド）から出す。"""
        def run():
            for name, last in transfers:
                self.m._emit_started(IP, name, 1, "download", name)
                self.m._emit_progress(IP, name, 1, 1, "download", name)
                if last == "interrupted":
                    self.m._emit_interrupted(IP, name, "download", name)
                else:
                    self.m._emit_complete(IP, name, 1, 1, "download", name)
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

    def test_transfer_notices_stop_piling_up_while_the_gui_is_busy(self):
        self._from_worker([("f-%d.bin" % i,
                            "interrupted" if i % 5 == 4 else "complete")
                           for i in range(BURST)])

        self.assertLessEqual(len(self.emitted), CAP + 1,
                             "転送の通知が上限で頭打ちにならない（%d 件が配送待ち）"
                             % len(self.emitted))
        started = self._names("started", self.emitted)
        closed = (self._names("complete", self.emitted)
                  + self._names("interrupted", self.emitted))
        self.assertTrue(started)
        self.assertEqual(sorted(started), sorted(closed),
                         "行が開始だけ／完了だけの片側になっている")

        self._drain()
        self.assertEqual(sorted(self._names("started", self.delivered)),
                         sorted(started))
        omitted = [m for m in self.activity if "省略" in m]
        self.assertEqual(len(omitted), 1,
                         "省略の通知が 1 行でない: %r" % (self.activity,))

        before = len(self.delivered)      # はけた後は、また通常どおり届く
        self._from_worker([("after.bin", "complete")])
        self._drain()
        self.assertEqual([k for k, fn in self.delivered[before:]],
                         ["started", "progress", "complete"])

    def test_nothing_is_omitted_when_the_gui_keeps_up(self):
        for i in range(CAP):
            self._from_worker([("ok-%d.bin" % i, "complete")])
            self._drain()
        self.assertEqual(len(self._names("started", self.delivered)), CAP)
        self.assertEqual(len(self._names("complete", self.delivered)), CAP)
        self.assertEqual([m for m in self.activity if "省略" in m], [])


class FtpTransferFloodPanelTest(unittest.TestCase):
    """実際の RETR の連打でも、配送待ちが上限で止まり、行が片側にならないこと。"""

    PANEL_CAP = 20
    STALL = 2.0

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtCore import Qt
        from core.config_manager import ConfigManager
        from ui.ftp_server_panel import FTPServerPanel
        work = tempfile.mkdtemp(prefix="netbelt-ftp-transfer-cap-")
        self.panel = FTPServerPanel(
            config_manager=ConfigManager(os.path.join(work, "config.json")))
        self.m = self.panel.ftp_server
        self.m.max_pending_notices = self.PANEL_CAP
        root = os.path.join(work, "root")
        os.makedirs(root)
        with open(os.path.join(root, "a.txt"), "wb") as f:
            f.write(b"x")
        self.assertTrue(self.m.start(port=0, root_dir=root,
                                     username="u", password="p"))
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

    def _flood(self, stop):
        try:
            while not stop.is_set():
                c = ftplib.FTP()
                c.connect("127.0.0.1", self.m.port, timeout=5)
                c.login("u", "p")
                c.voidcmd("TYPE I")
                for _ in range(20):
                    if stop.is_set():
                        break
                    d = c.transfercmd("RETR a.txt")
                    while d.recv(4096):
                        pass
                    d.close()
                    c.voidresp()
                c.close()
        except Exception:
            return

    def test_a_flood_of_downloads_is_capped_and_rows_are_closed(self):
        stop = threading.Event()
        threads = [threading.Thread(target=self._flood, args=(stop,), daemon=True)
                   for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(self.STALL)      # GUI（このスレッド）は回さない
        stop.set()
        for t in threads:
            t.join(10)

        self.assertLessEqual(
            self.emitted, 4 * self.PANEL_CAP,
            "GUI が止まっている間に転送の通知が %d 件積み上がった" % self.emitted)

        for _ in range(50):
            self.app.processEvents()
        h = self.panel.history
        statuses = [h.item(r, 5).text() for r in range(h.rowCount())]
        self.assertTrue(statuses, "履歴に 1 行も残っていない")
        self.assertEqual([s for s in statuses if s != "完了"], [],
                         "閉じられないまま残った行がある: %r" % (statuses,))
        self.assertEqual(self.panel._active, {})


if __name__ == "__main__":
    unittest.main()
