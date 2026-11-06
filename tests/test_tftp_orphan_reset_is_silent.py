# -*- coding: utf-8 -*-
"""要求だけ送ってポートを閉じた相手（重複要求の敗者）が、失敗として通知されないことを確かめる。

転送用ソケットから最初の ACK/OACK を閉じたポートへ送ると、Windows では次の
recvfrom が WinError 10054 になる。これは socket.timeout ではないので、確立前でも
外側の except へ落ちて『アップロード失敗 / ダウンロード失敗: [WinError 10054]』を
出していた。送信元ポートを変えて再送する機器では、勝者の転送は成功しているのに
同じ (ip, ファイル名, 方向) の行が「エラー」で閉じられ、台帳からも外れていた。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from core.tftp_server import TFTPServer, TFTPServerManager, OP_ACK, OP_DATA  # noqa: E402


def _req(op, filename, **opts):
    pkt = struct.pack("!H", op) + filename.encode() + b"\x00octet\x00"
    for k, v in opts.items():
        pkt += k.encode() + b"\x00" + str(v).encode() + b"\x00"
    return pkt


def _send_and_close(port, pkt):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(pkt, ("127.0.0.1", port))
    s.close()


class OrphanResetIsSilentTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-orphan-")
        with open(os.path.join(self.root, "r.bin"), "wb") as f:
            f.write(b"R" * 700)
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda *a: self.events.append(a))
        self.srv.start()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()

    def _errors(self):
        return [e for e in self.events if e[0] == "protocol_error"]

    def test_orphan_wrq_from_a_closed_port_reports_nothing(self):
        _send_and_close(self.port, _req(2, "w.bin"))
        time.sleep(2.0)
        self.assertEqual(self._errors(), [])

    def test_orphan_rrq_with_options_from_a_closed_port_reports_nothing(self):
        _send_and_close(self.port, _req(1, "r.bin", blksize=512))
        time.sleep(2.0)
        self.assertEqual(self._errors(), [])

    def test_orphan_rrq_without_options_from_a_closed_port_reports_nothing(self):
        _send_and_close(self.port, _req(1, "r.bin"))
        time.sleep(2.0)
        self.assertEqual(self._errors(), [])


class OrphanResetLeavesTheRealTransferAloneTest(unittest.TestCase):
    def test_real_upload_is_not_closed_as_an_error_by_an_orphan(self):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        root = tempfile.mkdtemp(prefix="netbelt-tftp-ledger-")
        m = TFTPServerManager()
        events, completed = [], []
        m.protocol_event.connect(lambda ip, fn, reason, d: events.append((fn, reason)))
        m.transfer_complete.connect(lambda ip, fn, dn, t, d: completed.append(fn))
        self.assertTrue(m.start(port=0, root_dir=root))
        self.addCleanup(m.stop)
        port = m._srv.port

        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        c.sendto(_req(2, "cfg"), ("127.0.0.1", port))
        _ack0, worker = c.recvfrom(1024)
        c.sendto(struct.pack("!HH", OP_DATA, 1) + b"C" * 512, worker)
        self.assertEqual(struct.unpack("!HH", c.recvfrom(1024)[0][:4]), (OP_ACK, 1))

        # 同じ機器が送信元ポートを変えて同じ WRQ を再送し、旧ポートは閉じた
        _send_and_close(port, _req(2, "cfg"))
        time.sleep(1.0)
        app.processEvents()

        c.sendto(struct.pack("!HH", OP_DATA, 2) + b"end", worker)
        self.assertEqual(struct.unpack("!HH", c.recvfrom(1024)[0][:4]), (OP_ACK, 2))
        limit = time.monotonic() + 3
        while not completed and time.monotonic() < limit:
            app.processEvents()
            time.sleep(0.05)
        app.processEvents()
        self.assertEqual(completed, ["cfg"])
        self.assertEqual(events, [], "成功した転送に偽の失敗が出た")


if __name__ == "__main__":
    unittest.main()
