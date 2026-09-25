"""FTP の進捗が、終わった転送の名前・方向で出ないこと。

進捗はデータチャネル（_ProgressDTP）の送受信ごとに出し、名前と方向は
ftp_RETR / ftp_STOR が制御接続へ覚えさせた _tx_name / _tx_dir を使う。
転送が終わってもそれを消していなかったので、ftp_STOR を通らないデータ
転送（名前をサーバが決める STOU、一覧の LIST / NLST）でも進捗が走り、
同じ制御接続で直前に行った転送の名前・方向で出た。
実測（1 本の接続で RETR down.bin を完了させてから STOU）:
('progress', 'down.bin', 'download') が 7 件出た。LIST でも同じ名前で
1 件出て、そのバイト数は一覧の大きさだった（機器のアップロード中に
別の行の進捗が巻き戻ったように見える）。

直し方: 転送が終わる 4 つのコールバック（on_file_sent / on_file_received /
on_incomplete_file_sent / on_incomplete_file_received）で _tx_name と
_tx_path を消す。_emit_tx_progress は名前が無ければ何も出さないので、
STOU も一覧も古い名前を拾わなくなる。
"""
import ftplib
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

CHUNK = 64 * 1024
CHUNKS = 8


class FtpStaleTransferProgressTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        self.addCleanup(self.m.stop)
        self.events = []
        self.m.transfer_started.connect(
            lambda ip, fn, total, d: self.events.append(("started", fn, d)))
        self.m.transfer_progress.connect(
            lambda ip, fn, done, total, d: self.events.append(("progress", fn, d)))
        self.m.transfer_complete.connect(
            lambda ip, fn, done, total, d: self.events.append(("complete", fn, d)))
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-stale-progress-")
        with open(os.path.join(self.root, "down.bin"), "wb") as f:
            f.write(b"d" * (CHUNK * CHUNKS))
        self.assertTrue(self.m.start(port=0, root_dir=self.root,
                                     username="u", password="p"))

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.02)

    def _login(self):
        c = ftplib.FTP()
        c.connect("127.0.0.1", self.m.port, timeout=5)
        self.addCleanup(c.close)
        c.login("u", "p")
        c.voidcmd("TYPE I")
        return c

    def _finish_a_download(self, c):
        """同じ制御接続で RETR を 1 本終わらせ、そこまでの通知を流す"""
        d = c.transfercmd("RETR down.bin")
        d.settimeout(5)
        while d.recv(65536):
            pass
        d.close()
        c.voidresp()
        self._pump(0.4)
        self.assertIn(("complete", "down.bin", "download"), self.events)
        return len(self.events)

    def _stale(self, mark):
        return [e for e in self.events[mark:]
                if e[0] == "progress" and e[1] == "down.bin"]

    def test_an_unnamed_upload_does_not_reuse_the_finished_transfer_name(self):
        c = self._login()
        mark = self._finish_a_download(c)

        d = c.transfercmd("STOU")       # ftp_STOR を通らないアップロード
        for _ in range(CHUNKS):
            d.sendall(b"u" * CHUNK)
            self._pump(0.25)            # 進捗の間引き（約 200ms）を越える間隔
        d.close()
        c.voidresp()
        self._pump(0.4)

        self.assertEqual(
            self._stale(mark), [],
            "終わった RETR の名前で STOU の進捗が出た: %r" % (self.events[mark:],))

    def test_a_listing_does_not_reuse_the_finished_transfer_name(self):
        c = self._login()
        mark = self._finish_a_download(c)

        lines = []
        c.retrlines("LIST", lines.append)
        self._pump(0.4)

        self.assertTrue(lines, "一覧が返っていない")
        self.assertEqual(
            self._stale(mark), [],
            "終わった RETR の名前で一覧の進捗が出た: %r" % (self.events[mark:],))

    def test_a_real_upload_still_reports_its_own_progress(self):
        c = self._login()
        mark = self._finish_a_download(c)

        d = c.transfercmd("STOR up.bin")
        for _ in range(CHUNKS):
            d.sendall(b"x" * CHUNK)
            self._pump(0.25)
        d.close()
        c.voidresp()
        self._pump(0.4)

        after = self.events[mark:]
        self.assertIn(("started", "up.bin", "upload"), after)
        self.assertTrue([e for e in after if e[:2] == ("progress", "up.bin")],
                        "本物のアップロードの進捗まで消えた: %r" % (after,))
        self.assertIn(("complete", "up.bin", "upload"), after)


if __name__ == "__main__":
    unittest.main()
