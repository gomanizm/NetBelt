# -*- coding: utf-8 -*-
"""転送用ポートへ届いた大きすぎるデータグラムで、進行中の転送が切れないことを確かめる。

WRQ は xs.recvfrom(blksize + 4)、RRQ の ACK 待ちは xs.recvfrom(516) で受けていた。
Windows では受信バッファより大きいデータグラムが届くと recvfrom 自体が
WinError 10040（WSAEMSGSIZE）を上げるので、送信元（TID）の照合より前に
外側の except へ落ち、『アップロード失敗』で転送が中断していた。第三者が
転送用ポートへ 4000 バイトを 1 発送るだけで、正規の相手は ERROR を受け取っていた。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from core.tftp_server import TFTPServer, OP_ACK, OP_DATA  # noqa: E402


def _wrq(filename, mode=b"octet"):
    return b"\x00\x02" + filename.encode() + b"\x00" + mode + b"\x00"


def _rrq(filename, mode=b"octet"):
    return b"\x00\x01" + filename.encode() + b"\x00" + mode + b"\x00"


class OversizedDatagramTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-big-")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda *a: self.events.append(a))
        self.srv.start()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        return c

    def _errors(self):
        return [e for e in self.events if e[0] == "protocol_error"]

    def test_oversized_datagram_from_a_third_party_does_not_abort_an_upload(self):
        c = self._client()
        c.sendto(_wrq("big.cfg"), ("127.0.0.1", self.port))
        ack0, worker = c.recvfrom(1024)
        self.assertEqual(struct.unpack("!HH", ack0[:4]), (OP_ACK, 0))
        first = b"A" * 512
        c.sendto(struct.pack("!HH", OP_DATA, 1) + first, worker)
        self.assertEqual(struct.unpack("!HH", c.recvfrom(1024)[0][:4]), (OP_ACK, 1))

        intruder = self._client()
        intruder.sendto(b"\x00\x03" + b"X" * 4000, worker)
        time.sleep(0.5)

        c.sendto(struct.pack("!HH", OP_DATA, 2) + b"end", worker)
        reply, _ = c.recvfrom(1024)
        self.assertEqual(struct.unpack("!HH", reply[:4]), (OP_ACK, 2),
                         "大きすぎるデータグラムで転送が中断した: %r" % (reply,))
        time.sleep(0.3)
        with open(os.path.join(self.root, "big.cfg"), "rb") as f:
            self.assertEqual(f.read(), first + b"end")
        self.assertEqual(self._errors(), [])

    def test_oversized_datagram_from_a_third_party_does_not_abort_a_download(self):
        body = b"B" * 1500
        with open(os.path.join(self.root, "dl.bin"), "wb") as f:
            f.write(body)
        c = self._client()
        c.sendto(_rrq("dl.bin"), ("127.0.0.1", self.port))
        data1, worker = c.recvfrom(1024)
        self.assertEqual(struct.unpack("!HH", data1[:4]), (OP_DATA, 1))

        intruder = self._client()
        intruder.sendto(b"\x00\x04" + b"Y" * 4000, worker)
        time.sleep(0.5)

        received = data1[4:]
        block = 1
        while True:
            c.sendto(struct.pack("!HH", OP_ACK, block), worker)
            if block > 1 and len(pkt) - 4 < 512:
                break
            pkt, _ = c.recvfrom(1024)
            op, block = struct.unpack("!HH", pkt[:4])
            self.assertEqual(op, OP_DATA, "大きすぎるデータグラムで転送が中断した: %r" % (pkt,))
            received += pkt[4:]
        self.assertEqual(received, body)
        time.sleep(0.3)
        self.assertEqual(self._errors(), [])


if __name__ == "__main__":
    unittest.main()
