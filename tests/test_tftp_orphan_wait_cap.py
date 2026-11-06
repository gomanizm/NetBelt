# -*- coding: utf-8 -*-
"""長い timeout を申告した孤児の要求が、ワーカー枠を長く握れないことを確かめる。

未確立の要求（最初の DATA / OACK への ACK が来ない）が枠を握る時間は、
(再送回数 + 1) x 相手が申告した timeout だった。timeout は 1〜255 秒まで受諾するので、
timeout=255 の WRQ を 16 本送るだけで約 25 分、どの要求も server busy で断られていた。

直し方: 確立までの待ちだけを ESTABLISH_WAIT_MAX_SECONDS で打ち切る。確立後の
転送は従来どおり申告どおりの timeout で待つ。テストでは上限を短くして確かめる。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from core.tftp_server import TFTPServer, OP_OACK  # noqa: E402


def _req(op, filename, **opts):
    pkt = struct.pack("!H", op) + filename.encode() + b"\x00octet\x00"
    for k, v in opts.items():
        pkt += k.encode() + b"\x00" + str(v).encode() + b"\x00"
    return pkt


class OrphanWaitCapTest(unittest.TestCase):
    CAP = 2.0

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-cap-")
        with open(os.path.join(self.root, "r.bin"), "wb") as f:
            f.write(b"R" * 700)
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda *a: self.events.append(a))
        self.srv.ESTABLISH_WAIT_MAX_SECONDS = self.CAP
        self.srv.start()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()

    def test_default_cap_is_at_most_thirty_seconds(self):
        self.assertLessEqual(TFTPServer.ESTABLISH_WAIT_MAX_SECONDS, 30.0)
        # 既定の待ち（timeout 2 秒 x 6 回）より短くはしない
        self.assertGreaterEqual(TFTPServer.ESTABLISH_WAIT_MAX_SECONDS, 12.0)

    def _orphan(self, pkt):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        c.sendto(pkt, ("127.0.0.1", self.port))
        reply, _ = c.recvfrom(1024)
        self.assertEqual(struct.unpack("!H", reply[:2])[0], OP_OACK)
        self.assertIn(b"timeout\x00255", reply)

    def _wait_workers_gone(self, limit):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            with self.srv._workers_lock:
                if not self.srv._workers:
                    return True
            time.sleep(0.1)
        return False

    def test_orphan_wrq_with_a_long_timeout_frees_its_slot(self):
        self._orphan(_req(2, "w.bin", timeout=255))
        self.assertTrue(self._wait_workers_gone(self.CAP + 3),
                        "timeout=255 の孤児 WRQ が枠を握り続けている")
        self.assertEqual([e for e in self.events if e[0] == "protocol_error"], [])

    def test_orphan_rrq_with_a_long_timeout_frees_its_slot(self):
        self._orphan(_req(1, "r.bin", timeout=255))
        self.assertTrue(self._wait_workers_gone(self.CAP + 3),
                        "timeout=255 の孤児 RRQ が枠を握り続けている")
        self.assertEqual([e for e in self.events if e[0] == "protocol_error"], [])


if __name__ == "__main__":
    unittest.main()
