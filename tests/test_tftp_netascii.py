"""TFTP の netascii モードが RFC 1350 どおり変換されることを確認する。

モードは解析だけして無視し、常に octet として保存・送信していた。
実測: netascii の WRQ で b"A\\r\\x00B\\r\\nC" を送ると、そのままの
バイト列が保存され「完了」も出る。RRQ では b"x\\ny\\rz" が変換なしで
送られる（RFC 1350 なら b"x\\r\\ny\\r\\x00z"）。

netascii では、回線上の CR LF が改行、CR NUL が CR を表す。受信時は
その逆変換を行い、送信時は改行を CR LF、CR を CR NUL に置き換える。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

OP_RRQ, OP_WRQ, OP_DATA, OP_ACK = 1, 2, 3, 4


def _req(op, filename, mode, opts=None):
    pkt = struct.pack("!H", op) + filename.encode() + b"\x00" + mode + b"\x00"
    for k, v in (opts or {}).items():
        pkt += k.encode() + b"\x00" + str(v).encode() + b"\x00"
    return pkt


class TftpNetasciiTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-na-")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda kind, ip, payload:
                              self.events.append((kind, payload)))
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.port = self.srv.port

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        return c

    def _upload(self, mode, blocks):
        """blocks（bytes のリスト。最後は blksize 未満）を送り切る。"""
        c = self._client()
        c.sendto(_req(OP_WRQ, "na.txt", mode), ("127.0.0.1", self.port))
        ack, srvaddr = c.recvfrom(1024)
        self.assertEqual(ack, b"\x00\x04\x00\x00")
        for i, chunk in enumerate(blocks, start=1):
            c.sendto(struct.pack("!HH", OP_DATA, i) + chunk, srvaddr)
            ack, _ = c.recvfrom(1024)
            self.assertEqual(ack, struct.pack("!HH", OP_ACK, i))
        with open(os.path.join(self.root, "na.txt"), "rb") as f:
            return f.read()

    def _download(self, mode):
        c = self._client()
        c.sendto(_req(OP_RRQ, "out.txt", mode), ("127.0.0.1", self.port))
        got = b""
        while True:
            data, srvaddr = c.recvfrom(2048)
            self.assertEqual(data[:2], b"\x00\x03")
            got += data[4:]
            c.sendto(b"\x00\x04" + data[2:4], srvaddr)
            if len(data[4:]) < 512:
                return got

    # --- 受信（WRQ） ---

    def test_netascii_upload_decodes_crlf_and_crnul(self):
        stored = self._upload(b"netascii", [b"A\r\x00B\r\nC"])
        self.assertEqual(stored, b"A\rB\nC",
                         "netascii の CR NUL / CR LF が復号されていない")

    def test_netascii_upload_handles_cr_split_across_blocks(self):
        """CR がブロック境界で分かれても、次ブロック先頭の LF/NUL と組になること。"""
        first = b"x" * 511 + b"\r"          # 512 バイトちょうど（続きあり）
        stored = self._upload(b"netascii", [first, b"\nend"])
        self.assertEqual(stored, b"x" * 511 + b"\nend")

    def test_octet_upload_is_untouched(self):
        payload = b"A\r\x00B\r\nC"
        self.assertEqual(self._upload(b"octet", [payload]), payload,
                         "octet の内容が変わっている")

    # --- 送信（RRQ） ---

    def test_netascii_download_encodes_newline_and_cr(self):
        with open(os.path.join(self.root, "out.txt"), "wb") as f:
            f.write(b"x\ny\rz")
        self.assertEqual(self._download(b"netascii"), b"x\r\ny\r\x00z",
                         "netascii の送信で改行/CR が変換されていない")

    def test_netascii_download_keeps_block_size_after_expansion(self):
        """変換で伸びた分もブロック長 512 に収めて送ること（最終判定が狂わない）。"""
        body = b"\n" * 600                  # 変換後 1200 バイト = 512 + 512 + 176
        with open(os.path.join(self.root, "out.txt"), "wb") as f:
            f.write(body)
        self.assertEqual(self._download(b"netascii"), b"\r\n" * 600)

    def test_octet_download_is_untouched(self):
        with open(os.path.join(self.root, "out.txt"), "wb") as f:
            f.write(b"x\ny\rz")
        self.assertEqual(self._download(b"octet"), b"x\ny\rz")


    def _wait_event(self, kind, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            hit = [p for k, p in self.events if k == kind]
            if hit:
                return hit[-1]
            time.sleep(0.02)
        self.fail("%s が出ていない: %r" % (kind, self.events))

    def test_netascii_oack_tsize_is_the_transferred_size(self):
        """RFC 2349: tsize は実際に転送されるオクテット数を返すこと。"""
        body = b"line\n" * 3                # 15 バイト -> netascii で 18 バイト
        with open(os.path.join(self.root, "out.txt"), "wb") as f:
            f.write(body)
        c = self._client()
        c.sendto(_req(OP_RRQ, "out.txt", b"netascii", {"tsize": "0"}),
                 ("127.0.0.1", self.port))
        pkt, srvaddr = c.recvfrom(2048)
        self.assertEqual(pkt[:2], b"\x00\x06", "OACK が返っていない: %r" % pkt)
        fields = pkt[2:].split(b"\x00")
        opts = dict(zip(fields[0::2], fields[1::2]))
        c.sendto(b"\x00\x04\x00\x00", srvaddr)      # ACK(0)
        data, srvaddr = c.recvfrom(2048)
        c.sendto(b"\x00\x04" + data[2:4], srvaddr)
        self.assertEqual(len(data[4:]), 18, "前提: 変換後は 18 バイト")
        self.assertEqual(opts.get(b"tsize"), b"18",
                         "tsize が変換前のサイズのまま: %r" % opts)

    def test_netascii_progress_never_exceeds_its_total(self):
        """進捗の分母も変換後のバイト数にすること（100% を超えない）。"""
        body = b"line\n" * 3
        with open(os.path.join(self.root, "out.txt"), "wb") as f:
            f.write(body)
        got = self._download(b"netascii")
        self.assertEqual(len(got), 18, "前提: 変換後は 18 バイト")
        filename, done, total, direction = self._wait_event("transfer_complete")
        self.assertEqual(done, len(got))
        self.assertEqual(total, len(got),
                         "進捗の分母が変換前のサイズのまま: %d/%d" % (done, total))


if __name__ == "__main__":
    unittest.main()
