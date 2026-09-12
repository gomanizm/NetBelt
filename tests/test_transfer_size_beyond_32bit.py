"""2GiB を超える転送サイズが、通知の途中で丸められないことを検証する。

FTP / TFTP の転送通知は pyqtSignal(str, str, int, str) 等で宣言されていた。
PyQt の int は C++ の int（32bit）なので、5GiB (5368709120) を渡すと
1GiB (1073741824) に化け、3GiB は負値になる。例外は出ず黙って丸まるため、
利用者には「5GiB のファイルが 1.0GB・進捗 100%」「レートが負」と見える。

SFTPManager は同じ問題を object 型で回避済み（sftp_manager.py:21）。
FTP/TFTP のサーバ側通知も同じ扱いに揃える。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

FIVE_GIB = 5 * 1024 * 1024 * 1024
THREE_GIB = 3 * 1024 * 1024 * 1024


class TransferSizeBeyond32BitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _managers(self):
        from core.ftp_server import FTPServerManager
        from core.tftp_server import TFTPServerManager
        return [FTPServerManager(), TFTPServerManager()]

    def test_started_keeps_the_total_of_a_5gib_file(self):
        for mgr in self._managers():
            got = []
            mgr.transfer_started.connect(
                lambda ip, fn, total, d: got.append(total))
            mgr.transfer_started.emit("192.0.2.1", "big.bin", FIVE_GIB,
                                      "download")
            self.assertEqual(got, [FIVE_GIB],
                             "%s: 転送サイズが丸められた" % type(mgr).__name__)

    def test_progress_keeps_bytes_past_2gib(self):
        for mgr in self._managers():
            got = []
            mgr.transfer_progress.connect(
                lambda ip, fn, done, total, d: got.append((done, total)))
            mgr.transfer_progress.emit("192.0.2.1", "big.bin", THREE_GIB,
                                       FIVE_GIB, "download")
            self.assertEqual(got, [(THREE_GIB, FIVE_GIB)],
                             "%s: 進捗が丸められた" % type(mgr).__name__)

    def test_complete_keeps_bytes_past_2gib(self):
        for mgr in self._managers():
            got = []
            mgr.transfer_complete.connect(
                lambda ip, fn, done, total, d: got.append((done, total)))
            mgr.transfer_complete.emit("192.0.2.1", "big.bin", FIVE_GIB,
                                       FIVE_GIB, "download")
            self.assertEqual(got, [(FIVE_GIB, FIVE_GIB)],
                             "%s: 完了サイズが丸められた" % type(mgr).__name__)

    def test_ftp_emit_helper_keeps_the_total(self):
        """FTP は _emit_started/_emit_progress を通るので、その経路も見る。"""
        from core.ftp_server import FTPServerManager
        mgr = FTPServerManager()
        started, progress = [], []
        mgr.transfer_started.connect(
            lambda ip, fn, total, d: started.append(total))
        mgr.transfer_progress.connect(
            lambda ip, fn, done, total, d: progress.append(done))
        mgr._emit_started("192.0.2.1", "big.bin", FIVE_GIB, "download")
        mgr._emit_progress("192.0.2.1", "big.bin", THREE_GIB, FIVE_GIB,
                           "download")
        self.assertEqual(started, [FIVE_GIB])
        self.assertEqual(progress, [THREE_GIB])

    def test_panel_shows_a_5gib_file_as_5gb(self):
        """パネルの表示（利用者が見る側）が 1.0GB にならないこと。"""
        from ui.tftp_server_panel import TFTPServerPanel
        from ui.ftp_server_panel import FTPServerPanel
        for panel in (TFTPServerPanel(), FTPServerPanel()):
            mgr = getattr(panel, "ftp_server", None) or panel.tftp_server
            mgr.transfer_started.emit("192.0.2.1", "big.bin", FIVE_GIB,
                                      "download")
            self.app.processEvents()
            self.assertEqual(panel.history.item(0, 3).text(), "5.0GB",
                             "%s: 表示サイズが丸められた" % type(panel).__name__)
            mgr.transfer_progress.emit("192.0.2.1", "big.bin", THREE_GIB,
                                       FIVE_GIB, "download")
            self.app.processEvents()
            self.assertEqual(panel.history.item(0, 5).text(), "60%",
                             "%s: 進捗が丸められた" % type(panel).__name__)


if __name__ == "__main__":
    unittest.main()
