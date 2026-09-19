"""束ねた接続の片方が先に終わっても、残りの接続の通知が別の転送の行へ付かないこと。

FTP の転送台帳（マネージャの _tx）は (IP, 実ファイルのパス, 方向) を鍵に
表示名を持ち、同じパスへの複数の接続は 1 件（画面の 1 行）に束ねる。
進捗・完了・中断の表示名は台帳から引き、鍵が無いときは basename に戻していた。
束ねた接続のうち先に終わった方が鍵を外すと、残った接続の通知は basename に
戻り、同じ相手が並行して転送している別フォルダの同名ファイルの行へ付いた。
実測（実パネル、すべて 127.0.0.1）: c1 が RETR a/x.bin（表示名 x.bin）、
c2 と c3 が RETR b/x.bin（表示名 /b/x.bin。1 件に束ねられる）。c2 を先に
受け終えてから c3 を受け終えると、c3 の完了が "x.bin" として通知され、
まだ 1 バイトも受け取っていない a の行が「完了」になった。

直し方: 接続（ハンドラ）ごとにも、その転送の表示名を覚える
（ftp_RETR / ftp_STOR で開始を通知したときに、束ねられた場合は既存の行の
表示名を受け取る）。台帳に鍵が無いときは basename ではなくこれを使う。
"""
import ftplib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

USER = "u"
PASSWORD = "p"
SIZE = 8 * 1024 * 1024


class FtpBundledConnectionDisplayTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        from ui.ftp_server_panel import FTPServerPanel
        cfg = mock.Mock()
        cfg.get_server_settings.return_value = {}
        self.panel = FTPServerPanel(config_manager=cfg)
        self.m = self.panel.ftp_server
        self.addCleanup(self.m.stop)
        self.events = []
        self.m.transfer_started.connect(
            lambda ip, fn, total, d: self.events.append(("started", fn, None)))
        self.m.transfer_progress.connect(
            lambda ip, fn, done, total, d: self.events.append(("progress", fn, done)))
        self.m.transfer_complete.connect(
            lambda ip, fn, done, total, d: self.events.append(("complete", fn, done)))
        self.m.transfer_interrupted.connect(
            lambda ip, fn, d: self.events.append(("interrupted", fn, None)))
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-bundled-")
        for sub in ("a", "b"):
            os.makedirs(os.path.join(self.root, sub))
            with open(os.path.join(self.root, sub, "x.bin"), "wb") as f:
                f.write(b"x" * SIZE)
        self.assertTrue(self.m.start(port=0, root_dir=self.root,
                                     username=USER, password=PASSWORD))
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            try:
                c.close()
            except Exception:
                pass

    def _client(self):
        c = ftplib.FTP()
        c.connect("127.0.0.1", self.m.port, timeout=5)
        c.login(USER, PASSWORD)
        c.voidcmd("TYPE I")
        self.clients.append(c)
        return c

    def _pump(self, cond=None, seconds=3.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if cond is not None and cond():
                return True
            time.sleep(0.02)
        self.app.processEvents()
        return cond() if cond is not None else True

    def _rows(self):
        h = self.panel.history
        return [tuple(h.item(r, c).text() if h.item(r, c) else ""
                      for c in range(6)) for r in range(h.rowCount())]

    def _names(self, kind):
        return [fn for k, fn, _ in self.events if k == kind]

    @staticmethod
    def _drain(d, slow_reads=0):
        """データ接続を最後まで読む。slow_reads 回は間を空けて少しずつ読む。"""
        d.settimeout(5)
        got = 0
        for _ in range(slow_reads):
            time.sleep(0.25)     # 進捗の間引き（約 200ms）を越える
            chunk = d.recv(65536)
            if not chunk:
                break
            got += len(chunk)
        while True:
            chunk = d.recv(65536)
            if not chunk:
                break
            got += len(chunk)
        d.close()
        return got

    def test_the_last_bundled_download_does_not_touch_the_other_folder_row(self):
        c1, c2, c3 = self._client(), self._client(), self._client()
        d1 = c1.transfercmd("RETR a/x.bin")
        self._pump(lambda: len(self._names("started")) >= 1)
        d2 = c2.transfercmd("RETR b/x.bin")
        self._pump(lambda: len(self._names("started")) >= 2)
        d3 = c3.transfercmd("RETR b/x.bin")     # 同じパス。台帳では 1 件に束ねる
        self._pump(seconds=0.5)
        self.assertEqual(self._names("started"), ["x.bin", "/b/x.bin"],
                         "前提: 同じパスの 2 本目は 1 件に束ねられる")

        # 束ねた片方（c2）を先に最後まで受け取る
        self.assertEqual(self._drain(d2), SIZE)
        c2.voidresp()
        self._pump(lambda: "/b/x.bin" in self._names("complete"))
        self.assertEqual(self._rows()[1][5], "完了")
        mark = len(self.events)

        # 残った c3 を少しずつ受け取る。a（c1）はまだ受け取っていない
        self.assertEqual(self._drain(d3, slow_reads=4), SIZE)
        c3.voidresp()
        self._pump(lambda: len(self._names("complete")) >= 2)
        after = self.events[mark:]
        completes = [fn for k, fn, _ in after if k == "complete"]
        self.assertEqual(completes, ["/b/x.bin"],
                         "束ねた c3 の完了が別フォルダの同名ファイルとして通知された: %r"
                         % completes)
        self.assertIn("/b/x.bin", [fn for k, fn, _ in after if k == "progress"],
                      "c3 の進捗が c3 の表示名で出ていない: %r" % after)
        self.assertNotEqual(self._rows()[0][5], "完了",
                            "受け取っていない a の行が完了になった: %r" % self._rows())
        self.assertIn(("127.0.0.1", "x.bin", "download"), self.panel._active,
                      "a の行の追跡が外れた")

        # a を最後まで受け取ると、a の行が完了になる
        self.assertEqual(self._drain(d1), SIZE)
        c1.voidresp()
        self._pump(lambda: self._rows()[0][5] == "完了")
        self.assertEqual(self._rows()[0][5], "完了")
        self.assertEqual(self.m._tx, {})


if __name__ == "__main__":
    unittest.main()
