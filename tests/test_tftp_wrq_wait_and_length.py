# -*- coding: utf-8 -*-
"""WRQ の受信ループの、確立までの待ちの上限と DATA の長さの上限を確かめる。

- 未確立の WRQ へ、相手（または第三者）が小さいデータグラムを 1 秒より短い間隔で
  送り続けると、受信の待ちが切れないので確立までの上限が効かず、枠を握り続けていた。
- 合意した blksize を超える DATA は書き込まない（読み捨てる）。
- 長さの上限は DATA にだけ効かせる。blksize を小さく合意したときも、相手の
  長めの ERROR は打ち切りとして扱う。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from core.tftp_server import TFTPServer, OP_ACK, OP_DATA, OP_ERROR  # noqa: E402


def _wrq(filename, **opts):
    pkt = b"\x00\x02" + filename.encode() + b"\x00octet\x00"
    for k, v in opts.items():
        pkt += k.encode() + b"\x00" + str(v).encode() + b"\x00"
    return pkt


class WrqReceiveLoopTest(unittest.TestCase):
    CAP = 2.0

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-wrqloop-")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda *a: self.events.append(a))
        self.srv.ESTABLISH_WAIT_MAX_SECONDS = self.CAP
        self.srv.start()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        return c

    def _workers_held(self):
        with self.srv._workers_lock:
            return len(self.srv._workers)

    def _hold_with_junk(self, sender_is_requester):
        c = self._client()
        c.sendto(_wrq("junk.bin"), ("127.0.0.1", self.port))
        _, worker = c.recvfrom(1024)
        sender = c if sender_is_requester else self._client()
        end = time.monotonic() + self.CAP + 3
        while time.monotonic() < end:
            sender.sendto(b"\x00", worker)
            time.sleep(0.3)
            if self._workers_held() == 0:
                return True
        return False

    def test_junk_from_the_requester_does_not_extend_the_establish_wait(self):
        self.assertTrue(self._hold_with_junk(True),
                        "本人からの小さいデータグラムで未確立の WRQ が枠を握り続けている")
        self.assertEqual([e for e in self.events if e[0] == "protocol_error"], [])

    def test_junk_from_a_third_party_does_not_extend_the_establish_wait(self):
        self.assertTrue(self._hold_with_junk(False),
                        "第三者のデータグラムで未確立の WRQ が枠を握り続けている")

    def test_data_longer_than_blksize_from_the_peer_is_not_written(self):
        c = self._client()
        c.sendto(_wrq("long.cfg"), ("127.0.0.1", self.port))
        ack0, worker = c.recvfrom(1024)
        self.assertEqual(struct.unpack("!HH", ack0[:4]), (OP_ACK, 0))
        c.sendto(struct.pack("!HH", OP_DATA, 1) + b"X" * 600, worker)
        time.sleep(0.3)
        c.sendto(struct.pack("!HH", OP_DATA, 1) + b"end", worker)
        reply, _ = c.recvfrom(1024)
        self.assertEqual(struct.unpack("!HH", reply[:4]), (OP_ACK, 1))
        time.sleep(0.3)
        with open(os.path.join(self.root, "long.cfg"), "rb") as f:
            self.assertEqual(f.read(), b"end")

    def test_long_error_with_a_small_blksize_interrupts_the_upload(self):
        c = self._client()
        c.sendto(_wrq("small.cfg", blksize=8), ("127.0.0.1", self.port))
        _, worker = c.recvfrom(1024)
        c.sendto(struct.pack("!HH", OP_DATA, 1) + b"12345678", worker)
        reply, _ = c.recvfrom(1024)
        self.assertEqual(struct.unpack("!HH", reply[:4]), (OP_ACK, 1))
        c.sendto(struct.pack("!HH", OP_ERROR, 3) + b"Disk full\x00", worker)
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            if any(e[0] == "interrupted" for e in self.events):
                break
            time.sleep(0.05)
        self.assertTrue(any(e[0] == "interrupted" for e in self.events),
                        "blksize を超える長さの ERROR が読み捨てられた: %r" % (self.events,))


if __name__ == "__main__":
    unittest.main()
