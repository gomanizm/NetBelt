"""TFTP の timeout オプション（RFC 2349）が転送ソケットに反映されることを確認する。

OACK で timeout を受諾しておきながら、転送ソケットは常に既定の 2 秒
のままだった。実測: timeout=30 を受諾した直後から 2 秒間隔で再送し、
約 12 秒で諦める。フラッシュ書込の遅い機器が長い timeout を要求しても
守られず、転送が打ち切られる。値の範囲検証（1〜255）も無く、不正な
値をそのまま echo していた。
"""
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

OP_WRQ, OP_OACK = 2, 6


def _wrq(filename, **opts):
    body = struct.pack("!H", OP_WRQ) + filename.encode() + b"\x00octet\x00"
    for k, v in opts.items():
        body += k.encode() + b"\x00" + str(v).encode() + b"\x00"
    return body


def _oack_opts(pkt):
    parts = pkt[2:].split(b"\x00")
    return dict(zip(parts[0::2], parts[1::2]))


class TftpTimeoutOptionTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-tmo-")
        self.srv = TFTPServer(port=0, root_dir=self.root)
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.port = self.srv.port

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(5)
        return c

    def test_negotiated_timeout_governs_retransmission(self):
        """timeout=4 を受諾したら、既定の 2 秒では OACK を再送しないこと。"""
        c = self._client()
        c.sendto(_wrq("a.txt", timeout=4), ("127.0.0.1", self.port))
        oack, srvaddr = c.recvfrom(1024)
        self.assertEqual(oack[:2], b"\x00\x06")
        self.assertEqual(_oack_opts(oack)[b"timeout"], b"4")
        # DATA を送らずに待つ。既定の 2 秒で再送されるなら 3 秒以内に届く
        c.settimeout(3)
        with self.assertRaises(socket.timeout,
                               msg="受諾した timeout ではなく既定の 2 秒で再送している"):
            c.recvfrom(1024)

    def test_out_of_range_timeout_is_not_accepted(self):
        """1〜255 の外や数値でない timeout は OACK で受諾しないこと。"""
        for bad in ("0", "256", "abc", "-1"):
            c = self._client()
            c.sendto(_wrq("b.txt", timeout=bad, blksize=1024), ("127.0.0.1", self.port))
            oack, _ = c.recvfrom(1024)
            self.assertEqual(oack[:2], b"\x00\x06")
            self.assertNotIn(b"timeout", _oack_opts(oack),
                             "不正な timeout=%s をそのまま受諾している" % bad)

    def test_valid_timeout_is_echoed_as_an_integer(self):
        c = self._client()
        c.sendto(_wrq("c.txt", timeout="007"), ("127.0.0.1", self.port))
        oack, _ = c.recvfrom(1024)
        self.assertEqual(_oack_opts(oack)[b"timeout"], b"7")


if __name__ == "__main__":
    unittest.main()
