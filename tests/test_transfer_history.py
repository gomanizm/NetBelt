"""転送履歴の行ごと進捗表示（進行中の転送枠を廃止）の検証。複数同時転送で各行が独立すること。"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class TransferHistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panels(self):
        from ui.tftp_server_panel import TFTPServerPanel
        from ui.ftp_server_panel import FTPServerPanel
        return [TFTPServerPanel(), FTPServerPanel()]

    def test_no_progress_area(self):
        for p in self._panels():
            self.assertFalse(hasattr(p, "progress_bar"))
            self.assertFalse(hasattr(p, "progress_label"))

    def test_concurrent_transfers_have_independent_rows(self):
        for p in self._panels():
            p._on_tx_started("192.0.2.10", "a.bin", 1000, "download")
            p._on_tx_started("192.0.2.20", "b.bin", 2000, "download")
            p._on_tx_progress("192.0.2.10", "a.bin", 500, 1000, "download")
            p._on_tx_progress("192.0.2.20", "b.bin", 500, 2000, "download")
            self.assertEqual(p.history.rowCount(), 2)
            self.assertEqual(p.history.item(0, 5).text(), "50%")   # A: 500/1000
            self.assertEqual(p.history.item(1, 5).text(), "25%")   # B: 500/2000
            # A 完了は B に影響しない
            p._on_tx_complete("192.0.2.10", "a.bin", 1000, 1000, "download")
            self.assertEqual(p.history.item(0, 5).text(), "完了")
            self.assertEqual(p.history.item(1, 5).text(), "25%")

    def test_upload_unknown_total_shows_ongoing(self):
        # アップロード(総サイズ不明 total=0)は % を出せないので「転送中」表示
        for p in self._panels():
            p._on_tx_started("192.0.2.30", "up.bin", 0, "upload")
            p._on_tx_progress("192.0.2.30", "up.bin", 4096, 0, "upload")
            self.assertEqual(p.history.item(0, 5).text(), "転送中")


if __name__ == "__main__":
    unittest.main()
