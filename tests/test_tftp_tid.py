"""TFTP の転送 ID (TID) 検証と、不正な長さのパケットへの耐性を確認する。

RFC 1350 は、転送が確立したあとのパケットは相手の TID (IP:port) が
一致するものだけを受け付けるよう求めている。照合が無いと、同一セグメントの
任意ホストが期待ブロック番号の DATA を1発撃つだけで、進行中のアップロードへ
任意データを混入させられる（UDP なので送信元詐称も容易）。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

from core.tftp_server import TFTPServer

OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5
ERR_UNKNOWN_TID = 5


def _wrq(filename, mode=b"octet"):
    return b"\x00\x02" + filename.encode() + b"\x00" + mode + b"\x00"


def _rrq(filename, mode=b"octet"):
    return b"\x00\x01" + filename.encode() + b"\x00" + mode + b"\x00"


def _data(block, payload):
    return struct.pack("!HH", OP_DATA, block) + payload


class TftpTidTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-tid-")
        self.srv = TFTPServer(port=0, root_dir=self.root)
        self.srv.start()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()

    def _client(self, timeout=3):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(timeout)
        self.addCleanup(c.close)
        return c

    def _begin_upload(self, filename):
        """WRQ を送って ACK(block 0) を受け、サーバの転送ソケットアドレスを返す。"""
        c = self._client()
        c.sendto(_wrq(filename), ("127.0.0.1", self.port))
        ack, srvaddr = c.recvfrom(1024)
        self.assertEqual(struct.unpack("!H", ack[:2])[0], OP_ACK)
        self.assertEqual(struct.unpack("!H", ack[2:4])[0], 0)
        return c, srvaddr

    def test_data_from_other_source_is_rejected(self):
        """別ホスト（別ポート）からの DATA は受理せず、書き込まれないこと。"""
        legit, srvaddr = self._begin_upload("victim.cfg")

        # 割り込み役: 別のソケット（＝別 TID）から期待ブロック番号の DATA を撃つ
        attacker = self._client()
        attacker.sendto(_data(1, b"INJECTED-BY-ATTACKER"), srvaddr)
        time.sleep(0.3)

        # 正規のクライアントが本来のデータを送って転送を完了させる
        legit.sendto(_data(1, b"legitimate-config"), srvaddr)
        ack, _ = legit.recvfrom(1024)
        self.assertEqual(struct.unpack("!H", ack[2:4])[0], 1)
        time.sleep(0.3)

        with open(os.path.join(self.root, "victim.cfg"), "rb") as f:
            got = f.read()
        self.assertNotIn(b"INJECTED", got, "別TIDからのデータが書き込まれた")
        self.assertEqual(got, b"legitimate-config")

    def test_error_unknown_tid_is_returned(self):
        """割り込み元へは ERROR code 5 (Unknown transfer ID) を返すこと。"""
        _legit, srvaddr = self._begin_upload("tid.cfg")

        attacker = self._client()
        attacker.sendto(_data(1, b"nope"), srvaddr)
        try:
            resp, _ = attacker.recvfrom(1024)
        except socket.timeout:
            self.fail("割り込み元へ ERROR が返らなかった")
        self.assertEqual(struct.unpack("!H", resp[:2])[0], OP_ERROR)
        self.assertEqual(struct.unpack("!H", resp[2:4])[0], ERR_UNKNOWN_TID)

    def test_short_datagram_does_not_abort_transfer(self):
        """2バイト未満の空データグラムで転送が壊れないこと。"""
        legit, srvaddr = self._begin_upload("short.cfg")

        # 長さ検証が無いと struct.unpack が struct.error を投げ、転送が中断していた
        legit.sendto(b"", srvaddr)
        legit.sendto(b"\x00", srvaddr)
        time.sleep(0.2)

        legit.sendto(_data(1, b"still-works"), srvaddr)
        ack, _ = legit.recvfrom(1024)
        self.assertEqual(struct.unpack("!H", ack[2:4])[0], 1)
        time.sleep(0.3)
        with open(os.path.join(self.root, "short.cfg"), "rb") as f:
            self.assertEqual(f.read(), b"still-works")

    def test_download_returns_error_to_other_source(self):
        """ダウンロード時、別 TID からの ACK には ERROR 5 を返すこと。

        「転送が狂わないこと」だけを見ると、ブロック番号の一致で結果的に通って
        しまい、TID を照合していない実装でも緑になる。割り込み元への応答を
        見ることで、照合しているかどうかを実際に判定する。
        """
        with open(os.path.join(self.root, "img.bin"), "wb") as f:
            f.write(b"A" * 1200)   # 512 + 512 + 176 → 3 ブロック

        c = self._client()
        c.sendto(_rrq("img.bin"), ("127.0.0.1", self.port))
        blk1, srvaddr = c.recvfrom(2048)
        self.assertEqual(struct.unpack("!H", blk1[2:4])[0], 1)

        attacker = self._client()
        attacker.sendto(struct.pack("!HH", OP_ACK, 1), srvaddr)
        try:
            resp, _ = attacker.recvfrom(1024)
        except socket.timeout:
            self.fail("別TIDの ACK に ERROR が返らなかった（TID を照合していない）")
        self.assertEqual(struct.unpack("!H", resp[:2])[0], OP_ERROR)
        self.assertEqual(struct.unpack("!H", resp[2:4])[0], ERR_UNKNOWN_TID)

    def test_download_continues_for_legitimate_client(self):
        """割り込みがあっても、正規クライアントの転送は続くこと。"""
        with open(os.path.join(self.root, "img2.bin"), "wb") as f:
            f.write(b"B" * 1200)

        c = self._client()
        c.sendto(_rrq("img2.bin"), ("127.0.0.1", self.port))
        blk1, srvaddr = c.recvfrom(2048)
        self.assertEqual(struct.unpack("!H", blk1[2:4])[0], 1)

        attacker = self._client()
        attacker.sendto(struct.pack("!HH", OP_ACK, 1), srvaddr)
        time.sleep(0.2)

        c.sendto(struct.pack("!HH", OP_ACK, 1), srvaddr)
        blk2, _ = c.recvfrom(2048)
        self.assertEqual(struct.unpack("!H", blk2[2:4])[0], 2,
                         "割り込みで正規の転送が狂った")

    def test_normal_upload_still_works(self):
        """既存の正常系を壊していないこと。"""
        c, srvaddr = self._begin_upload("normal.cfg")
        c.sendto(_data(1, b"hello"), srvaddr)
        ack, _ = c.recvfrom(1024)
        self.assertEqual(struct.unpack("!H", ack[2:4])[0], 1)
        time.sleep(0.3)
        with open(os.path.join(self.root, "normal.cfg"), "rb") as f:
            self.assertEqual(f.read(), b"hello")


if __name__ == "__main__":
    unittest.main()
