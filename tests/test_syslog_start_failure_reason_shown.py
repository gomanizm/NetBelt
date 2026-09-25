"""Syslog 受信の起動失敗の理由が画面に出ることの回帰テスト。

何が起きていたか（実測、基準 f4cad23）: core/syslog_receiver.py は bind に
失敗すると error_occurred で理由を出していたが、この信号を繋いでいる場所が
リポジトリ全体に 1 つも無かった（ui/syslog_panel.py:945 の
set_syslog_receiver は receiver を代入するだけ、ui/main_window.py も
message_received しか繋いでいない）。指定 UDP ポートを別ソケットで占有して
から受信開始を押すと、

    出たダイアログ: []
    error_occurred の中身: ['UDP ポート 52123 のバインドに失敗: [WinError 10048] ...']
    ステータス表示: '🔴 停止中'

となり、利用者からは「押したが何も起こらず停止中のまま、理由も出ない」
状態になっていた。UDP/TCP を両方選んで片方だけ失敗したときは、成功した側の
「受信を開始しました」だけが出て、落ちた側の理由は消えていた。
FTP/TFTP/SFTP の各パネルは同じ error_occurred を _on_error へ繋いで
QMessageBox.critical を出しており、Syslog だけが抜けていた。

どう直したか: set_syslog_receiver で receiver.error_occurred を
_on_receiver_error へ繋ぎ、他のサーバーパネルと同じく QMessageBox.critical で
理由を出す。終了処理中（_closing）はモーダルを開かない点も他パネルに合わせた。
"""
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _busy_udp_port():
    """使用中の UDP ポートを 1 つ作って (ソケット, ポート) を返す"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    return sock, sock.getsockname()[1]


def _busy_tcp_port():
    """使用中の TCP ポートを 1 つ作って (ソケット, ポート) を返す"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("0.0.0.0", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _free_udp_port():
    """誰も使っていない UDP ポート番号"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class SyslogStartFailureReasonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from core.config_manager import ConfigManager
        from core.syslog_receiver import SyslogReceiver
        from ui.syslog_panel import SyslogPanel

        directory = tempfile.mkdtemp(prefix="netbelt-syslogerr-")
        config = ConfigManager(config_path=os.path.join(directory,
                                                        "config.json"))
        panel = SyslogPanel(config_manager=config)
        self.addCleanup(panel.close)
        receiver = SyslogReceiver()
        self.addCleanup(receiver.stop)
        panel.set_syslog_receiver(receiver)
        return panel

    def _record_boxes(self):
        """QMessageBox の呼び出しを (種類, 見出し, 本文) で集める"""
        boxes = []

        def recorder(kind):
            def handler(*args, **kwargs):
                boxes.append((kind, args[1], args[2]))
            return handler

        patches = [
            mock.patch("ui.syslog_panel.QMessageBox.information",
                       side_effect=recorder("info")),
            mock.patch("ui.syslog_panel.QMessageBox.warning",
                       side_effect=recorder("warn")),
            mock.patch("ui.syslog_panel.QMessageBox.critical",
                       side_effect=recorder("crit")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return boxes

    def test_single_protocol_failure_shows_the_reason(self):
        blocker, busy = _busy_udp_port()
        self.addCleanup(blocker.close)
        panel = self._panel()
        panel.udp_check.setChecked(True)
        panel.tcp_check.setChecked(False)
        panel.udp_port_spin.setValue(busy)

        boxes = self._record_boxes()
        panel._toggle_receiver()

        criticals = [b for b in boxes if b[0] == "crit"]
        self.assertTrue(
            criticals,
            "起動できなかった理由がダイアログに出ていません: %r" % (boxes,))
        self.assertIn("UDP", criticals[0][2])
        self.assertIn(str(busy), criticals[0][2])

    def test_partial_failure_shows_the_failed_protocol(self):
        blocker, busy_tcp = _busy_tcp_port()
        self.addCleanup(blocker.close)
        panel = self._panel()
        panel.udp_check.setChecked(True)
        panel.tcp_check.setChecked(True)
        panel.udp_port_spin.setValue(_free_udp_port())
        panel.tcp_port_spin.setValue(busy_tcp)

        boxes = self._record_boxes()
        panel._toggle_receiver()

        criticals = [b for b in boxes if b[0] == "crit"]
        self.assertTrue(
            criticals,
            "片方だけ失敗したときに理由が出ていません: %r" % (boxes,))
        self.assertIn("TCP", criticals[0][2])
        self.assertIn(str(busy_tcp), criticals[0][2])

    def test_adding_a_protocol_while_running_shows_the_reason(self):
        blocker, busy_tcp = _busy_tcp_port()
        self.addCleanup(blocker.close)
        panel = self._panel()
        panel.udp_check.setChecked(True)
        panel.tcp_check.setChecked(False)
        panel.udp_port_spin.setValue(_free_udp_port())
        panel.tcp_port_spin.setValue(busy_tcp)

        boxes = self._record_boxes()
        panel._toggle_receiver()
        self.assertTrue(panel.syslog_receiver.is_running,
                        "UDP の受信を開始できませんでした")
        del boxes[:]

        panel._on_protocol_toggled("TCP", True)

        criticals = [b for b in boxes if b[0] == "crit"]
        self.assertTrue(
            criticals,
            "受信中に足したプロトコルの失敗理由が出ていません: %r" % (boxes,))
        self.assertIn(str(busy_tcp), criticals[0][2])
        self.assertFalse(panel.tcp_check.isChecked(),
                         "起動できなかったプロトコルのチェックが残っています")

    def test_no_modal_while_closing(self):
        blocker, busy = _busy_udp_port()
        self.addCleanup(blocker.close)
        panel = self._panel()
        panel._closing = True
        panel.udp_check.setChecked(True)
        panel.tcp_check.setChecked(False)
        panel.udp_port_spin.setValue(busy)

        boxes = self._record_boxes()
        panel._toggle_receiver()

        self.assertEqual([b for b in boxes if b[0] == "crit"], [],
                         "終了処理中にモーダルを開いています")

    def test_main_window_tells_the_syslog_panel_it_is_closing(self):
        from ui.main_window import MainWindow
        self.assertIn("syslog_panel", MainWindow._PANELS_TOLD_WHEN_CLOSING,
                      "Syslog パネルへ終了処理の印が渡されていません")


if __name__ == "__main__":
    unittest.main()
