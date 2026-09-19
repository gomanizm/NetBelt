"""アップロード（WRQ）中にクライアントが ERROR を送ったら、その場で中断として終えること。

WRQ の受信ループは、受け取ったパケットが DATA でなければ読み捨てていた
（op != OP_DATA なら continue）。機器が転送を打ち切って ERROR を送っても
ワーカーは気づかず、タイムアウトまで最後の ACK を再送し続け、その間
ファイルを開いたまま、同時転送の枠も 1 つ塞いだままにした。
実測（DATA 1 ブロックの後に ERROR を送った）: ワーカーはその後も ACK(1) を
2 秒おきに 5 回再送し、12 秒後にタイムアウトとして終わった（通知は
「アップロードがタイムアウト」の protocol_error）。

直し方: 相手の TID から ERROR を受けたら、停止要求と同じ中断の経路で終える。
確立後（最初の DATA を受けた後）なら interrupted を通知し、ACK 済みの分は
書き出して閉じる。確立前なら何も通知せずに撤退する（ファイルも作らない）。
別の TID から届いた ERROR は従来どおり受理しない。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

OP_WRQ, OP_DATA, OP_ACK, OP_ERROR = 2, 3, 4, 5


def _wrq(filename):
    return struct.pack("!H", OP_WRQ) + filename.encode() + b"\x00octet\x00"


def _error(code=0, message="aborted by device"):
    return struct.pack("!HH", OP_ERROR, code) + message.encode() + b"\x00"


class TftpWrqClientErrorTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-wrq-error-")
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

    def test_error_after_data_ends_the_upload_as_interrupted(self):
        c = self._client()
        c.sendto(_wrq("cfg.txt"), ("127.0.0.1", self.srv.port))
        ack, peer = c.recvfrom(1024)
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 0))
        c.sendto(struct.pack("!HH", OP_DATA, 1) + b"A" * 512, peer)
        ack, _ = c.recvfrom(1024)
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 1))

        c.sendto(_error(), peer)     # 機器が転送を打ち切った
        self.assertTrue(self._wait_until(lambda: not self._alive_workers()),
                        "ERROR を受けてもワーカーが終わらない")
        kinds = [k for k, _ in self.events]
        self.assertIn(("interrupted", ("cfg.txt", "upload")), self.events)
        self.assertNotIn("protocol_error", kinds,
                         "打ち切りがタイムアウト等の失敗として通知された: %r" % self.events)
        self.assertNotIn("transfer_complete", kinds)

        # ACK を再送し続けていない
        c.settimeout(2.5)
        with self.assertRaises(OSError):
            c.recvfrom(1024)
        # ACK 済みの分は書き出され、ファイルは閉じられている
        path = os.path.join(self.root, "cfg.txt")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"A" * 512)
        os.replace(path, path + ".moved")

    def test_error_before_any_data_withdraws_silently(self):
        c = self._client()
        c.sendto(_wrq("never.txt"), ("127.0.0.1", self.srv.port))
        _ack, peer = c.recvfrom(1024)
        c.sendto(_error(8, "option refused"), peer)
        self.assertTrue(self._wait_until(lambda: not self._alive_workers()),
                        "確立前の ERROR でもワーカーが終わらない")
        self.assertEqual(self.events, [])
        self.assertEqual(os.listdir(self.root), [])

    def test_error_from_another_tid_does_not_end_the_upload(self):
        c = self._client()
        c.sendto(_wrq("kept.txt"), ("127.0.0.1", self.srv.port))
        _ack, peer = c.recvfrom(1024)
        c.sendto(struct.pack("!HH", OP_DATA, 1) + b"B" * 512, peer)
        c.recvfrom(1024)

        stranger = self._client()
        stranger.sendto(_error(), peer)
        time.sleep(0.3)
        self.assertTrue(self._alive_workers(), "別の TID の ERROR で転送が終わった")

        c.sendto(struct.pack("!HH", OP_DATA, 2) + b"end", peer)
        ack, _ = c.recvfrom(1024)
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 2))
        self.assertTrue(self._wait_until(
            lambda: "transfer_complete" in [k for k, _ in self.events]))
        self.assertNotIn("interrupted", [k for k, _ in self.events])


if __name__ == "__main__":
    unittest.main()
