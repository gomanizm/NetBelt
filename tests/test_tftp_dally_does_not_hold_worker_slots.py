"""完了したアップロードの dally（最終 ACK 後の待ち）が、同時転送の枠を塞がないこと。

TFTP のワーカーは最終 ACK の後も、再送された最終 DATA に応えるため最低 6 秒
待つ。この待ちの間もワーカーは同時転送の枠（max_workers=16）に数えられて
いたので、1 台から小さいファイルを続けて置くだけで枠が埋まり、17 件目以降が
'server busy' で断られた。実測（1 台から 0.2 秒おきに 1 ブロックのファイルを
置く。通ったアップロードは正常に完了）: 40 件のうち 14 件が 'server busy'
で断られた（17 件目から、最初の待ちが明けるまで断られ続ける）。

直し方: dally に入ったスレッドは同時転送の枠から外し、別の上限
（max_dallying）を設けた一覧へ移す。その一覧が埋まっているときは待たずに
閉じる（転送は完了済みで、失うのは最終 ACK が落ちたときの再 ACK だけ）。
stop() は両方の一覧のスレッドを待つ。
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


class TftpDallySlotsTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-dally-slots-")
        self.srv = TFTPServer(port=0, root_dir=self.root)
        self.srv.start()
        self.addCleanup(self.srv.stop)

    def _upload(self, name, body=b"hostname r1\n"):
        """1 ブロックのファイルを置き、'ok' か受け取った ERROR の文言を返す。"""
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            c.settimeout(3)
            c.sendto(_wrq(name), ("127.0.0.1", self.srv.port))
            pkt, peer = c.recvfrom(1024)
            op = struct.unpack("!H", pkt[:2])[0]
            if op == OP_ERROR:
                return pkt[4:-1].decode(errors="replace")
            c.sendto(struct.pack("!HH", OP_DATA, 1) + body, peer)
            pkt, _ = c.recvfrom(1024)
            return "ok" if pkt[:4] == struct.pack("!HH", OP_ACK, 1) else repr(pkt)
        finally:
            c.close()

    def _alive(self):
        with self.srv._workers_lock:
            threads = list(self.srv._workers) + list(getattr(self.srv, "_dallying", []))
        return [t for t in threads if t.is_alive()]

    def test_back_to_back_small_uploads_are_not_refused(self):
        count = self.srv.max_workers + 6
        results = []
        for i in range(count):
            results.append(self._upload("cfg-%02d.txt" % i))
            time.sleep(0.2)
        refused = [(i, r) for i, r in enumerate(results) if r != "ok"]
        self.assertEqual(refused, [],
                         "完了済みの転送の待ちで枠が埋まり、断られた: %r" % refused)
        self.assertEqual(sorted(os.listdir(self.root)),
                         ["cfg-%02d.txt" % i for i in range(count)])

        # 待ちの最中のスレッドも stop() で止まる
        started = time.monotonic()
        self.srv.stop()
        self.assertLess(time.monotonic() - started, 3.0,
                        "待ちの最中のスレッドのぶん stop() が固まっている")
        self.assertEqual(self._alive(), [], "停止後もスレッドが残っている")

    def test_dallying_threads_have_their_own_cap(self):
        self.srv.max_dallying = 2
        for i in range(5):
            self.assertEqual(self._upload("capped-%d.txt" % i), "ok")
        time.sleep(0.3)
        with self.srv._workers_lock:
            dallying = [t for t in self.srv._dallying if t.is_alive()]
            workers = [t for t in self.srv._workers if t.is_alive()]
        self.assertLessEqual(len(dallying), 2,
                             "待ちのスレッドが上限を超えて残っている: %d" % len(dallying))
        self.assertEqual(workers, [], "完了した転送が同時転送の枠に残っている")


if __name__ == "__main__":
    unittest.main()
