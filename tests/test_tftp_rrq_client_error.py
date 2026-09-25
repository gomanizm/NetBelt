"""ダウンロード（RRQ）中にクライアントが ERROR を送ったら、その場で終えること。

アップロード（WRQ）側は相手の ERROR で中断するようにしたが、送信側の
_send_and_wait_ack は「期待した ACK ではないパケット」として ERROR を
読み捨てるだけだった。機器が転送を打ち切っても、ワーカーは最大
_retries 回 DATA を再送し続け、その間ファイルと同時転送の枠を掴んだまま
残る。最後には中断ではなく偽の失敗として通知される。
実測（1536 バイトを RRQ、block 1 を ACK して確立させてから TID の一致する
ERROR を送った）: そのあとも DATA を 5 回再送し、ワーカーが終わるまで
13.0 秒かかった。最後の通知は
('protocol_error', ('big.bin', 'ダウンロードがタイムアウト', 'download'))。

直し方: 相手の TID から ERROR を受けたら専用の例外を送出し、_handle_rrq は
停止要求と同じ枝で受ける。確立後なら interrupted を通知し、確立前
（重複 RRQ の敗者が受け取る ERROR 5 など）は従来どおり黙って撤退する。
別の TID から届いた ERROR は、これまでどおり受理しない。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

OP_RRQ, OP_DATA, OP_ACK, OP_ERROR = 1, 3, 4, 5
BLOCK = 512


def _rrq(filename):
    return struct.pack("!H", OP_RRQ) + filename.encode() + b"\x00octet\x00"


def _error(code=0, message="aborted by device"):
    return struct.pack("!HH", OP_ERROR, code) + message.encode() + b"\x00"


class TftpRrqClientErrorTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-rrq-error-")
        with open(os.path.join(self.root, "big.bin"), "wb") as f:
            f.write(b"z" * (BLOCK * 2 + 100))   # 3 ブロック（最後は半端）
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda k, ip, p: self.events.append((k, p)))
        self.srv.start()
        self.addCleanup(self.srv.stop)

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        return c

    def _alive_workers(self):
        with self.srv._workers_lock:
            threads = list(self.srv._workers) + list(getattr(self.srv, "_dallying", []))
        return [t for t in threads if t.is_alive()]

    def _wait_until(self, cond, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    def _start_download(self, c):
        """RRQ を出し、block 1 を ACK して転送を確立させる。相手の TID を返す"""
        c.sendto(_rrq("big.bin"), ("127.0.0.1", self.srv.port))
        pkt, peer = c.recvfrom(2048)
        self.assertEqual(struct.unpack("!HH", pkt[:4]), (OP_DATA, 1))
        c.sendto(struct.pack("!HH", OP_ACK, 1), peer)
        pkt, peer = c.recvfrom(2048)
        self.assertEqual(struct.unpack("!HH", pkt[:4]), (OP_DATA, 2))
        return peer

    def test_error_after_the_first_ack_ends_the_download_as_interrupted(self):
        c = self._client()
        peer = self._start_download(c)

        started = time.monotonic()
        c.sendto(_error(), peer)         # 機器が転送を打ち切った
        self.assertTrue(self._wait_until(lambda: not self._alive_workers()),
                        "ERROR を受けてもワーカーが終わらない")
        self.assertLess(time.monotonic() - started, 3.0,
                        "打ち切りに気づくまで再送を続けている")
        kinds = [k for k, _ in self.events]
        self.assertIn(("interrupted", ("big.bin", "download")), self.events)
        self.assertNotIn("protocol_error", kinds,
                         "打ち切りが失敗として通知された: %r" % (self.events,))
        self.assertNotIn("transfer_complete", kinds)

        c.settimeout(2.5)                # DATA を再送し続けていない
        with self.assertRaises(OSError):
            c.recvfrom(2048)

    def test_error_before_any_ack_withdraws_silently(self):
        c = self._client()
        c.sendto(_rrq("big.bin"), ("127.0.0.1", self.srv.port))
        pkt, peer = c.recvfrom(2048)
        self.assertEqual(struct.unpack("!HH", pkt[:4]), (OP_DATA, 1))

        c.sendto(_error(5, "Unknown transfer ID"), peer)
        self.assertTrue(self._wait_until(lambda: not self._alive_workers()),
                        "確立前の ERROR でもワーカーが終わらない")
        self.assertEqual(self.events, [],
                         "未確立の撤退で通知が出た: %r" % (self.events,))

    def test_error_from_another_tid_does_not_end_the_download(self):
        c = self._client()
        peer = self._start_download(c)

        stranger = self._client()
        stranger.sendto(_error(), peer)
        try:                             # 送信元へは ERROR 5 が返る
            resp, _ = stranger.recvfrom(2048)
            self.assertEqual(struct.unpack("!H", resp[:2])[0], OP_ERROR)
        except socket.timeout:
            self.fail("別 TID の ERROR に応答が無い")
        self.assertTrue(self._alive_workers(), "別の TID の ERROR で転送が終わった")

        c.sendto(struct.pack("!HH", OP_ACK, 2), peer)
        pkt, _ = c.recvfrom(2048)
        self.assertEqual(struct.unpack("!HH", pkt[:4]), (OP_DATA, 3))
        c.sendto(struct.pack("!HH", OP_ACK, 3), peer)
        self.assertTrue(self._wait_until(
            lambda: "transfer_complete" in [k for k, _ in self.events]))
        self.assertNotIn("interrupted", [k for k, _ in self.events])


if __name__ == "__main__":
    unittest.main()
