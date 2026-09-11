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
import unittest

sys.path.insert(0, "src")

OP_RRQ, OP_WRQ, OP_DATA, OP_ACK = 1, 2, 3, 4


def _req(op, filename, mode):
    return struct.pack("!H", op) + filename.encode() + b"\x00" + mode + b"\x00"


class TftpNetasciiTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-na-")
        self.srv = TFTPServer(port=0, root_dir=self.root)
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


if __name__ == "__main__":
    unittest.main()
