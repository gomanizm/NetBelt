"""FTP の未完了転送が、履歴に「転送中」のまま残らないことを確認する。

FTP は完了時にしか通知が無かったため、ABOR や接続断で終わった転送は
履歴行が途中の進捗（例: "1%"）で固定され、マネージャ側の _tx にも
鍵が残り続けた。_tx が残ると次の試行は既存行に束ねられてしまい、
再試行の開始も分からない。TFTP は同じ状況を transfer_interrupted で
通知しているので、FTP もそれに揃える。
"""
import io
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
import ftplib

sys.path.insert(0, "src")


class FtpInterruptedTransferTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-abort-")
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        # 無人テストで実ファイアウォール（UAC/ルール追加）を叩かないようスタブ必須
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        self.assertTrue(self.m.start(port=0, root_dir=self.root,
                                     username="u", password="p"))
        self.port = self.m.port
        time.sleep(0.3)

    def tearDown(self):
        self.m.stop()

    def _wait(self, predicate, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_a_download_cut_in_the_middle_is_reported_as_interrupted(self):
        """データ接続が途中で切れた取得を、中断として通知すること。"""
        with open(os.path.join(self.root, "big.bin"), "wb") as f:
            f.write(b"x" * (4 * 1024 * 1024))
        interrupted = []
        self.m.transfer_interrupted.connect(
            lambda ip, fn, d: interrupted.append((fn, d)))

        f = ftplib.FTP()
        f.connect("127.0.0.1", self.port, timeout=5)
        f.login("u", "p")
        conn = f.transfercmd("RETR big.bin")
        conn.recv(4096)
        conn.close()            # 受け取り切らずにデータ接続を閉じる
        try:
            f.close()
        except Exception:
            pass

        self.assertTrue(
            self._wait(lambda: interrupted),
            "未完了の取得が中断として通知されない")
        self.assertEqual(interrupted[0], ("big.bin", "download"))
        self.assertEqual(self.m._tx, {},
                         "中断した転送の鍵が残り、次の試行が新しい行にならない")

    def test_the_panel_finishes_the_row_on_an_interruption(self):
        """中断の通知で、履歴行を「中断」にして進行中から外すこと。"""
        from ui.ftp_server_panel import FTPServerPanel
        panel = FTPServerPanel()
        panel._on_tx_started("192.0.2.1", "big.bin", 1000, "download")
        panel._on_tx_progress("192.0.2.1", "big.bin", 10, 1000, "download")
        panel._on_transfer_interrupted("192.0.2.1", "big.bin", "download")
        self.assertEqual(panel.history.item(0, 5).text(), "中断")
        self.assertEqual(panel._active, {})

    def test_stopping_the_server_finishes_the_rows_left_running(self):
        """停止したら、進行中のまま残った行も確定させること。"""
        from ui.ftp_server_panel import FTPServerPanel
        panel = FTPServerPanel()
        panel._on_tx_started("192.0.2.1", "big.bin", 1000, "download")
        panel._on_server_stopped()
        self.assertEqual(panel.history.item(0, 5).text(), "中断")
        self.assertEqual(panel._active, {})

    def test_a_retry_after_an_interruption_gets_its_own_row(self):
        """中断のあとの再試行は、開始として通知すること（行を束ねない）。"""
        started = []
        self.m.transfer_started.connect(
            lambda ip, fn, total, d: started.append(fn))
        self.m._emit_started("192.0.2.1", "big.bin", 1000, "download")
        self.m._emit_interrupted("192.0.2.1", "big.bin", "download")
        self.m._emit_started("192.0.2.1", "big.bin", 1000, "download")
        self.assertEqual(started, ["big.bin", "big.bin"])


if __name__ == "__main__":
    unittest.main()
