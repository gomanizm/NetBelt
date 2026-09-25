"""機器が打ち切った転送を、利用者が止めたかのように記録しないこと。

TFTP パネルの中断のログは『[ip] 停止により中断: filename』で固定だった。
これは停止ボタンを押した場合の文面で、当時はその経路しか無かった。
いまは機器が ERROR を送って打ち切った転送（アップロード・ダウンロードの
どちらも）も同じ中断の経路を通るので、利用者が何もしていないのに
「停止により」と記録され、ログを後から読む人を誤らせる。
実測（WRQ で DATA を 1 ブロック送ってから ERROR を送った）:
『[127.0.0.1] 停止により中断: cfg.txt』。

直し方: 中断の理由は通知に含まれていないので（相手の打ち切りと停止を
見分ける手がかりがパネルには無い）、文言を理由に踏み込まない「転送中断」に
一般化する。FTP パネルの同じ場面の文言と揃う。行の状況列が「中断」に
なることは変えない。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

OP_WRQ, OP_DATA, OP_ACK, OP_ERROR = 2, 3, 4, 5


class TftpInterruptedLogWordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        from ui.tftp_server_panel import TFTPServerPanel
        cfg = mock.Mock()
        cfg.get_server_settings.return_value = {}
        self.panel = TFTPServerPanel(config_manager=cfg)
        self.addCleanup(self.panel.tftp_server.stop)

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.02)

    def _log(self):
        return self.panel.log_text.toPlainText()

    def test_an_upload_aborted_by_the_device_is_not_logged_as_a_stop(self):
        root = tempfile.mkdtemp(prefix="netbelt-tftp-wording-")
        self.assertTrue(self.panel.tftp_server.start(port=0, root_dir=root))
        port = self.panel.tftp_server._srv.port
        self._pump(0.2)

        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        c.sendto(struct.pack("!H", OP_WRQ) + b"cfg.txt\x00octet\x00",
                 ("127.0.0.1", port))
        _ack, peer = c.recvfrom(1024)
        c.sendto(struct.pack("!HH", OP_DATA, 1) + b"A" * 512, peer)
        c.recvfrom(1024)
        self._pump(0.3)
        c.sendto(struct.pack("!HH", OP_ERROR, 0) + b"aborted\x00", peer)

        deadline = time.time() + 5
        while time.time() < deadline and "中断" not in self._log():
            self._pump(0.1)
        log = self._log()

        self.assertIn("中断", log, "中断が記録されていない: %r" % log)
        self.assertIn("cfg.txt", log)
        self.assertNotIn("停止", log,
                         "機器が打ち切ったのに停止したことにされた: %r" % log)

    def test_the_row_is_still_marked_interrupted(self):
        ip, filename, direction = "192.0.2.10", "conf.bin", "upload"
        self.panel._on_tx_started(ip, filename, 4096, direction)
        row = self.panel._active[(ip, filename, direction)]["row"]

        self.panel._on_transfer_interrupted(ip, filename, direction)

        self.assertEqual(self.panel.history.item(row, 5).text(), "中断")
        self.assertIn(filename, self._log())
        self.assertNotIn("停止", self._log())


if __name__ == "__main__":
    unittest.main()
