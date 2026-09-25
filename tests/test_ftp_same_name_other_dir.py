"""別ディレクトリにある同名ファイルの並行転送を、別の転送として追跡すること。

FTP の転送台帳（マネージャの _tx）と画面の追跡（パネルの _active）は
(IP, basename, 方向) を鍵にしていた。同じ送信元から a/running.cfg と
b/running.cfg を並行して転送すると、同じ転送として 1 件にまとめられた。
実測（実パネルで 2 本の接続から並行 STOR）: 両方が転送中の時点で開始通知は
1 件、履歴も 1 行。a が完了するとその行は「完了」になり、台帳とパネルの
追跡が両方とも空になった。b はまだ転送中なのに以後の進捗は表示されず、
b の完了時に新しい行が作られた。ファイルの中身は正しく、ずれるのは表示だけ。

直し方: マネージャの台帳の鍵を、basename ではなく実ファイルのパス
（pyftpdlib が RETR/STOR と完了・中断の通知に渡す同じパス）にし、台帳に
その転送の表示名を持たせる。表示名はふだんどおり basename で、同じ相手・
同じ方向で別の場所の同名ファイルが転送中のときだけ、ルートからのパス
（例: /b/running.cfg）にする。パネルは表示名で行を追跡するので、これで
取り違えなくなる。シグナルの引数と、重ならないときの表示は変えない。
機器が 1 回の copy で同じファイルへ複数の接続を張る場合は、パスも同じなので
これまでどおり 1 行にまとまる。
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


class FtpSameNameOtherDirTest(unittest.TestCase):
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
        self.started = []
        self.m.transfer_started.connect(
            lambda ip, fn, total, d: self.started.append((fn, d)))
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-samename-")
        os.makedirs(os.path.join(self.root, "a"))
        os.makedirs(os.path.join(self.root, "b"))
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

    def test_parallel_uploads_of_the_same_name_are_two_transfers(self):
        c1, c2 = self._client(), self._client()
        d1 = c1.transfercmd("STOR a/running.cfg")
        d1.sendall(b"A" * 1000)
        self._pump(lambda: len(self.started) >= 1)
        d2 = c2.transfercmd("STOR b/running.cfg")
        d2.sendall(b"B" * 1000)

        self._pump(lambda: len(self.started) >= 2)
        self.assertEqual(len(self.started), 2,
                         "別ディレクトリの同名ファイルの開始が 1 件にまとめられた: %r"
                         % self.started)
        # 先の転送は従来どおり basename、重なった方だけルートからのパスで見せる
        self.assertEqual(self.started, [("running.cfg", "upload"),
                                        ("/b/running.cfg", "upload")])
        self.assertEqual([r[2] for r in self._rows()],
                         ["running.cfg", "/b/running.cfg"])

        # a だけ完了させる。b はまだ転送中として追跡され続けること
        d1.close()
        c1.voidresp()
        self._pump(lambda: self._rows()[0][5] == "完了")
        self.assertEqual(self._rows()[0][5], "完了")
        self.assertEqual(len(self.panel._active), 1,
                         "転送中の b まで追跡が外れた: %r" % self.panel._active)
        self.assertEqual(len(self.m._tx), 1,
                         "転送中の b まで台帳から外れた: %r" % self.m._tx)
        self.assertEqual(self._rows()[1][5], "転送中")

        d2.sendall(b"B" * 50000)
        d2.close()
        c2.voidresp()
        self._pump(lambda: self._rows()[1][5] == "完了")
        rows = self._rows()
        self.assertEqual(len(rows), 2, "b の完了で行が増えた: %r" % rows)
        self.assertEqual([r[5] for r in rows], ["完了", "完了"])
        self.assertEqual(self.panel._active, {})
        self.assertEqual(self.m._tx, {})
        with open(os.path.join(self.root, "b", "running.cfg"), "rb") as f:
            self.assertEqual(len(f.read()), 51000)

    def test_parallel_downloads_of_the_same_name_are_two_transfers(self):
        size = 8 * 1024 * 1024
        for sub in ("a", "b"):
            with open(os.path.join(self.root, sub, "x.bin"), "wb") as f:
                f.write(b"x" * size)
        c1, c2 = self._client(), self._client()
        d1 = c1.transfercmd("RETR a/x.bin")
        self._pump(lambda: len(self.started) >= 1)
        d2 = c2.transfercmd("RETR b/x.bin")
        # 受け取らずにおけば、送信側がバッファで詰まって転送中のまま残る
        self._pump(lambda: len(self.started) >= 2)
        self.assertEqual(self.started, [("x.bin", "download"),
                                        ("/b/x.bin", "download")],
                         "別ディレクトリの同名ファイルの開始が 1 件にまとめられた")

        # a だけ最後まで受け取る
        got = 0
        d1.settimeout(5)
        while True:
            chunk = d1.recv(65536)
            if not chunk:
                break
            got += len(chunk)
        d1.close()
        c1.voidresp()
        self.assertEqual(got, size)
        self._pump(lambda: self._rows()[0][5] == "完了")
        self.assertEqual(self._rows()[0][5], "完了")
        self.assertEqual(len(self.panel._active), 1,
                         "転送中の b まで追跡が外れた: %r" % self.panel._active)
        self.assertEqual(len(self.m._tx), 1,
                         "転送中の b まで台帳から外れた: %r" % self.m._tx)
        before = self._rows()[1][5]
        self.assertTrue(before.endswith("%"), "b の行が転送中でない: %r" % before)

        # b の進捗は b の行に出る
        time.sleep(0.3)   # 進捗の間引き（約 200ms）を越える
        d2.settimeout(5)
        for _ in range(64):
            d2.recv(65536)
        time.sleep(0.3)
        d2.recv(65536)
        self._pump(lambda: self._rows()[1][5] != before)
        self.assertNotEqual(self._rows()[1][5], before, "b の進捗が表示されない")
        self.assertNotEqual(self._rows()[1][5], "完了")
        d2.close()

    def test_a_file_in_a_folder_is_still_shown_by_its_name(self):
        """重ならなければ、フォルダの中のファイルも従来どおり basename で出る。"""
        c = self._client()
        d = c.transfercmd("STOR a/solo.cfg")
        d.sendall(b"S" * 100)
        d.close()
        c.voidresp()
        self._pump(lambda: self._rows() and self._rows()[0][5] == "完了")
        self.assertEqual(self.started, [("solo.cfg", "upload")])
        self.assertEqual(self._rows()[0][2], "solo.cfg")


if __name__ == "__main__":
    unittest.main()
