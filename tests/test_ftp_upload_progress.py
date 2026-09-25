"""FTP のアップロード（STOR）でも、転送中の進捗が通知されること。

進捗はデータチャネル（_ProgressDTP）の送受信ごとに出す作りで、受信側は
handle_read を上書きしていた。ところが pyftpdlib の DTPHandler はクラス定義の
時点で handle_read_event = handle_read と別名を束縛しており、ioloop が呼ぶのは
handle_read_event なので、上書きした handle_read は一度も呼ばれない。
実測: 1.5 秒かけて 960KiB を STOR しても transfer_progress は 0 件
（同じ条件の RETR では出る）。パネルのアップロードの行は、完了するまで
「転送中」のまま速度も出なかった。

直し方: 上書きした handle_read を handle_read_event にも束縛し直す
（親クラスと同じ別名の付け方）。
"""
import ftplib
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

CHUNK = 64 * 1024
CHUNKS = 15


class FtpUploadProgressTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        self.addCleanup(self.m.stop)
        self.progress = []
        self.complete = []
        self.m.transfer_progress.connect(
            lambda ip, fn, done, total, d: self.progress.append((fn, done, total, d)))
        self.m.transfer_complete.connect(
            lambda ip, fn, done, total, d: self.complete.append((fn, done, d)))
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-upload-progress-")
        self.assertTrue(self.m.start(port=0, root_dir=self.root,
                                     username="u", password="p"))

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.02)

    def test_progress_is_reported_while_uploading(self):
        c = ftplib.FTP()
        c.connect("127.0.0.1", self.m.port, timeout=5)
        self.addCleanup(c.close)
        c.login("u", "p")
        c.voidcmd("TYPE I")
        d = c.transfercmd("STOR up.bin")
        for _ in range(CHUNKS):          # 間引き（約 200ms）を越える間隔で少しずつ送る
            d.sendall(b"x" * CHUNK)
            self._pump(0.1)
        d.close()
        c.voidresp()
        self._pump(0.3)

        uploads = [p for p in self.progress if p[3] == "upload"]
        self.assertTrue(uploads, "アップロードの進捗が一度も通知されない")
        self.assertEqual({p[0] for p in uploads}, {"up.bin"})
        dones = [p[1] for p in uploads]
        self.assertEqual(dones, sorted(dones), "進捗が逆戻りした: %r" % dones)
        self.assertTrue(0 < dones[-1] <= CHUNK * CHUNKS)
        self.assertEqual(self.complete, [("up.bin", CHUNK * CHUNKS, "upload")])
        self.assertEqual(os.path.getsize(os.path.join(self.root, "up.bin")),
                         CHUNK * CHUNKS)


if __name__ == "__main__":
    unittest.main()
