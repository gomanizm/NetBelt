"""srv-02: WRQ の tsize に巨大な数を書かれると、TFTP パネルの表示が例外になる件。

何が起きていたか（実測、基準 470c538。127.0.0.1 のみ）: tsize に 400 桁の
数字を入れた WRQ を送り、返ってきた転送用ポートへ DATA(1) を 1 つ送ると、
パネルの _on_tx_started の中で _fmt_bytes() の n /= 1024.0 が
OverflowError（int too large to convert to float）になった。行を足した
あとで値を作るので、履歴には中身の空の行が残り、「転送開始」のログも出ない。
本番では main.py の例外フックが「予期しないエラー」のダイアログを出す。
TFTP は認証が無いので、届く相手なら誰でも繰り返し起こせる。
サーバ側は tsize を int() に通すだけで範囲を見ていなかった（負の値も通る）。

どう直したか: サーバは WRQ の tsize を 0 以上 2**63 未満のときだけ使い、
それ以外（負・巨大・数でない）は 0 ＝「大きさ不明」として扱う。表示側の
_fmt_bytes() も float へ直せない値では例外にせず「—」を返す。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import traceback
import unittest

sys.path.insert(0, "src")

OP_WRQ, OP_DATA, OP_ACK = 2, 3, 4
HUGE = "9" * 400


def _wrq(filename, tsize):
    return (struct.pack("!H", OP_WRQ) + filename.encode() + b"\x00octet\x00"
            + b"tsize\x00" + tsize.encode() + b"\x00")


def _upload(port, filename, tsize, payload=b"hello"):
    """WRQ を送り、1 ブロックだけの DATA を送って最後の ACK を受ける。"""
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(5)
    try:
        client.sendto(_wrq(filename, tsize), ("127.0.0.1", port))
        _reply, tid = client.recvfrom(1024)
        client.sendto(struct.pack("!HH", OP_DATA, 1) + payload, tid)
        ack, _ = client.recvfrom(1024)
        return struct.unpack("!HH", ack[:4])
    finally:
        client.close()


class WrqTsizeBoundsServerTest(unittest.TestCase):
    """サーバが申告された tsize をどう扱うか（通知に載る total）。"""

    def setUp(self):
        from core.tftp_server import TFTPServer
        self.events = []
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-tsize-")
        self.server = TFTPServer(
            port=0, root_dir=self.root,
            on_event=lambda kind, ip, payload: self.events.append((kind, payload)))
        self.server.start()
        self.addCleanup(self.server.stop)

    def _started_total(self, filename, tsize):
        self.assertEqual(_upload(self.server.port, filename, tsize), (OP_ACK, 1))
        for kind, payload in self.events:
            if kind == "transfer_started" and payload[0] == filename:
                return payload[1]
        self.fail("transfer_started が出ていない: %r" % (self.events,))

    def test_a_huge_tsize_is_treated_as_unknown(self):
        self.assertEqual(self._started_total("huge.cfg", HUGE), 0,
                         "400 桁の tsize をそのまま大きさとして渡している")

    def test_out_of_range_tsizes_are_treated_as_unknown(self):
        for i, tsize in enumerate(("-5", str(2 ** 63))):
            with self.subTest(tsize=tsize):
                self.assertEqual(self._started_total("range%d.cfg" % i, tsize), 0)

    def test_a_plausible_tsize_is_still_used(self):
        for i, tsize in enumerate(("0", "1500", str(2 ** 63 - 1))):
            with self.subTest(tsize=tsize):
                self.assertEqual(self._started_total("ok%d.cfg" % i, tsize),
                                 int(tsize))

    def test_a_non_numeric_tsize_is_still_unknown(self):
        self.assertEqual(self._started_total("text.cfg", "abc"), 0)


class WrqTsizeBoundsPanelTest(unittest.TestCase):
    """パネルが巨大な大きさで例外を出さず、行を作りきること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        from ui.tftp_server_panel import TFTPServerPanel
        d = tempfile.mkdtemp(prefix="netbelt-tftp-tsize-panel-")
        self.panel = TFTPServerPanel(config_manager=ConfigManager(
            config_path=os.path.join(d, "config.json")))
        self.root = os.path.join(d, "root")
        self.errors = []
        old_hook = sys.excepthook
        sys.excepthook = lambda t, v, tb: self.errors.append(
            "".join(traceback.format_exception(t, v, tb)))
        self.addCleanup(setattr, sys, "excepthook", old_hook)

    def _pump(self, seconds=1.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def test_fmt_bytes_does_not_raise_on_a_value_too_large_for_float(self):
        self.assertEqual(self.panel._fmt_bytes(10 ** 400), "—")
        self.assertEqual(self.panel._fmt_bytes(1536), "1.5KB")

    def test_a_row_is_completed_for_a_huge_total(self):
        self.panel._on_tx_started("192.0.2.5", "huge.cfg", 10 ** 400, "upload")
        self.assertEqual(self.errors, [])
        row = self.panel.history.rowCount() - 1
        self.assertEqual(self.panel.history.item(row, 2).text(), "huge.cfg",
                         "履歴の行が作りきられていない")

    def test_a_huge_tsize_over_the_wire_raises_nothing(self):
        """実際の WRQ でも、例外を出さず「転送開始」と「完了」が揃うこと。"""
        self.assertTrue(self.panel.tftp_server.start(0, self.root, True, True))
        self.addCleanup(self.panel.tftp_server.stop)
        port = self.panel.tftp_server._srv.port

        self.assertEqual(_upload(port, "wire.cfg", HUGE), (OP_ACK, 1))
        self._pump()

        self.assertEqual(self.errors, [], "表示の処理で例外が出た")
        self.assertEqual(self.panel.history.rowCount(), 1,
                         "空の行と完了の行に割れている")
        cells = [self.panel.history.item(0, c).text() for c in range(6)]
        self.assertEqual(cells[2], "wire.cfg")
        self.assertEqual(cells[5], "完了")
        self.assertIn("転送開始: wire.cfg", self.panel.log_text.toPlainText())


if __name__ == "__main__":
    unittest.main()
