"""FTP/TFTP/SFTP パネルのログと転送履歴に保持上限があることを確認する。

アクティビティログも転送履歴も、認証を通らない相手の要求だけで増やせる
（TFTP の存在しないファイルへの RRQ など）。上限が無いと、遠隔から叩き
続けるだけでメモリを食い潰せてしまう。Syslog が 1000 件で頭打ちになるのに
合わせ、サーバーパネル側にも上限を置く。

履歴の古い行を捨てると残りの行番号がずれるため、進行中の転送が覚えている
行番号も併せて繰り上がること（別の転送の行を上書きしないこと）まで見る。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class ServerPanelLogLimitsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panels(self):
        from ui.ftp_server_panel import FTPServerPanel
        from ui.sftp_server_panel import SFTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel
        return [FTPServerPanel(), SFTPServerPanel(), TFTPServerPanel()]

    def test_activity_log_stops_growing_at_the_limit(self):
        """ログ行数が上限を超えて増え続けないこと。"""
        for panel in self._panels():
            limit = panel.MAX_LOG_LINES
            for i in range(limit + 200):
                panel._add_log("line %d" % i)
            self.assertLessEqual(
                panel.log_text.document().blockCount(), limit,
                "%s のログが上限を超えた" % type(panel).__name__)
            # 直近の行は残っていること（古い側から捨てる）
            self.assertIn("line %d" % (limit + 199), panel.log_text.toPlainText())

    def _history_panels(self):
        from ui.ftp_server_panel import FTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel
        return [FTPServerPanel(), TFTPServerPanel()]

    def test_transfer_history_stops_growing_at_the_limit(self):
        """転送履歴の行数が上限を超えて増え続けないこと。"""
        for panel in self._history_panels():
            limit = panel.MAX_HISTORY_ROWS
            for i in range(limit + 200):
                panel._on_tx_complete("192.0.2.10", "f%d.bin" % i, 10, 10, "download")
            self.assertLessEqual(
                panel.history.rowCount(), limit,
                "%s の転送履歴が上限を超えた" % type(panel).__name__)

    def test_trimming_history_keeps_an_active_transfer_on_its_own_row(self):
        """履歴を切り詰めても、進行中の転送の進捗が自分の行へ書かれること。"""
        for panel in self._history_panels():
            limit = panel.MAX_HISTORY_ROWS
            name = type(panel).__name__
            # 先に捨てられる側の行を 20 行積んでから、進行中の転送を始める
            for i in range(20):
                panel._on_tx_complete("192.0.2.10", "old%d.bin" % i, 10, 10, "download")
            panel._on_tx_started("192.0.2.20", "live.bin", 1000, "download")
            # ちょうど 10 行ぶん溢れさせる（捨てられるのは old*.bin だけ）
            for i in range(limit - 21 + 10):
                panel._on_tx_complete("192.0.2.10", "new%d.bin" % i, 10, 10, "download")
            self.assertEqual(panel.history.rowCount(), limit, "%s の行数" % name)

            panel._on_tx_progress("192.0.2.20", "live.bin", 500, 1000, "download")
            live = [r for r in range(panel.history.rowCount())
                    if panel.history.item(r, 2) is not None
                    and panel.history.item(r, 2).text() == "live.bin"]
            self.assertEqual(len(live), 1, "%s の進行中の行が見つからない" % name)
            self.assertEqual(panel.history.item(live[0], 5).text(), "50%",
                             "%s の進捗が自分の行へ書かれていない" % name)
            # 別の行を巻き添えにしていないこと
            others = [r for r in range(panel.history.rowCount())
                      if r != live[0] and panel.history.item(r, 5) is not None
                      and panel.history.item(r, 5).text() == "50%"]
            self.assertEqual(others, [], "%s が別の転送の行を上書きした" % name)

    def test_history_can_be_cleared(self):
        """転送履歴を手動で空にできること。"""
        for panel in self._history_panels():
            for i in range(5):
                panel._on_tx_complete("192.0.2.10", "f%d.bin" % i, 10, 10, "download")
            self.assertGreater(panel.history.rowCount(), 0)
            panel._on_clear_history()
            self.assertEqual(panel.history.rowCount(), 0,
                             "%s の履歴が消えていない" % type(panel).__name__)


if __name__ == "__main__":
    unittest.main()
