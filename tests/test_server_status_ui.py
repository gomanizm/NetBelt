"""サーバー/受信の状態表示を全パネルで統一（停止中=赤・起動ボタンのみ / 起動中=青・停止ボタンのみ）。"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class ServerStatusUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _server_panels(self):
        from ui.tftp_server_panel import TFTPServerPanel
        from ui.ftp_server_panel import FTPServerPanel
        from ui.sftp_server_panel import SFTPServerPanel
        return [TFTPServerPanel(), FTPServerPanel(), SFTPServerPanel()]

    def test_only_start_button_visible_when_stopped(self):
        for p in self._server_panels():
            # 未起動時は起動ボタンだけが有効な選択肢（停止ボタンは隠れている）
            self.assertFalse(p.stop_btn.isVisibleTo(p))
            self.assertIn("停止中", p.status_label.text())
            self.assertIn("🔴", p.status_label.text())

    def test_snmp_trap_stopped_state(self):
        # SNMPPanel 単体生成は MIB バックグラウンド読み込みでヘッドレスが落ちるため
        # MainWindow 経由（既存テストと同じ生成経路）で検証する。
        from ui.main_window import MainWindow
        p = MainWindow().snmp_panel
        self.assertFalse(p.trap_stop_button.isVisibleTo(p))
        self.assertIn("停止中", p.trap_status_label.text())
        self.assertIn("🔴", p.trap_status_label.text())

    def test_syslog_has_receive_status_label(self):
        from ui.syslog_panel import SyslogPanel
        p = SyslogPanel()
        self.assertIn("停止中", p.recv_status_label.text())
        self.assertIn("🔴", p.recv_status_label.text())
        # サーバーパネルと同じ大ボタン構成（停止中は停止ボタン非表示）
        self.assertFalse(p.stop_btn.isVisibleTo(p))
        self.assertIn("受信開始", p.start_btn.text())


if __name__ == "__main__":
    unittest.main()
