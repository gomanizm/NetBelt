"""TFTP のアップロード完了で、保存先の予約を外すより先に完了を知らせることを検証する。

_handle_wrq は最終 ACK の後の待ち（_dally）へ入る前に予約を外すが、基準 16101ef は
外してから transfer_complete を出していた（src/core/tftp_server.py:457）。その間に
同じ相手・同じ名前の次の WRQ が確立すると、後続が先行と同じ台帳の行へ相乗りして
count が 2 になる。先行の完了で done=True になるので、後続のタイムアウトは
成功済みの重複要求として握り潰され（同 :886/:925）、保存先は後続の途中データなのに
履歴には先行の「完了」だけが残りうる。

実測（基準 16101ef）: 2 本のクライアントで同名 cfg.txt を 25 秒連打しても
成功 30506 回・File busy 0 回・count が 2 になった回数 0 で、自然状態では
一度も踏めなかった。_release_target の直後と transfer_complete の間には I/O が
無いためで、_release_target の直後に 0.5 秒の gate を入れて隙間を広げると
10 秒で 319/320 回踏み、後続の transfer_started が先行の完了より先に出た。
つまり読解としては正しく、機構は本物。

直し方: 2 文を入れ替えて、完了を知らせてから予約を外す。dally より前に外す性質
（同じファイルを置き直す機器を断らない）はそのまま保たれる。
"""
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, "src")

OP_DATA, OP_ACK, OP_ERROR = 3, 4, 5


def _wrq(filename, mode=b"octet"):
    return b"\x00\x02" + filename.encode() + b"\x00" + mode + b"\x00"


class TftpCompleteBeforeReleaseTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-order-")
        self.events = []
        # transfer_complete が届いた時点で保存先がまだ予約されているかを覚える
        self.reserved_at_complete = []
        self.srv = TFTPServer(port=0, root_dir=self.root, on_event=self._on_event)
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.port = self.srv.port

    def _on_event(self, kind, ip, payload):
        if kind == "transfer_complete":
            self.reserved_at_complete.append(set(self.srv._wrq_targets))
        self.events.append((kind, payload))

    # --- クライアント側の手順 ------------------------------------------

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        return c

    def _start_upload(self, c, filename="cfg.txt"):
        c.sendto(_wrq(filename), ("127.0.0.1", self.port))
        ack0, srvaddr = c.recvfrom(1024)
        self.assertEqual(ack0[:2], b"\x00\x04", "ACK0 が返らない: %r" % ack0)
        return srvaddr

    def _send_block(self, c, srvaddr, block, payload):
        c.sendto(b"\x00\x03" + struct.pack("!H", block) + payload, srvaddr)
        reply, _ = c.recvfrom(1024)
        return reply

    def _target_key(self, filename="cfg.txt"):
        return os.path.normcase(os.path.realpath(os.path.join(self.root, filename)))

    # --- 本題 ------------------------------------------------------------

    def test_the_target_is_still_reserved_when_completion_is_announced(self):
        c = self._client()
        srvaddr = self._start_upload(c)
        reply = self._send_block(c, srvaddr, 1, b"hostname R1")
        self.assertEqual(reply[:2], b"\x00\x04")
        deadline = time.monotonic() + 3
        while not self.reserved_at_complete and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.reserved_at_complete, "完了が通知されない: %s" % self.events)
        self.assertIn(self._target_key(), self.reserved_at_complete[0],
                      "完了を知らせる前に予約を外している（後続がこの行へ相乗りできる）")

    def test_a_following_upload_cannot_join_the_completed_transfer(self):
        """予約が外れる瞬間を広げて、後続が先行の完了より先に確立できるか見る"""
        released = threading.Event()
        gate = threading.Event()
        real_release = self.srv._release_target
        gated = {"done": False}

        def gated_release(target):
            real_release(target)
            if not gated["done"]:
                gated["done"] = True
                released.set()
                gate.wait(3.0)   # 隙間を広げる（自然状態では数マイクロ秒）

        self.srv._release_target = gated_release
        self.addCleanup(setattr, self.srv, "_release_target", real_release)
        self.addCleanup(gate.set)

        a = self._client()
        srvaddr_a = self._start_upload(a)
        # 最終ブロック。ACK が返った時点で書き込みは終わっている
        self.assertEqual(self._send_block(a, srvaddr_a, 1, b"first")[:2], b"\x00\x04")
        self.assertTrue(released.wait(3.0), "予約が外れない")

        # 予約が外れた直後に、同じ名前の次のアップロードを確立させる
        b = self._client()
        srvaddr_b = self._start_upload(b)
        reply = self._send_block(b, srvaddr_b, 1, b"z" * 512)
        self.assertEqual(reply[:2], b"\x00\x04", "後続が確立できない: %r" % reply)
        gate.set()
        time.sleep(0.3)

        kinds = [k for k, _ in self.events]
        self.assertIn("transfer_complete", kinds, "先行の完了が無い: %s" % self.events)
        starts = [i for i, (k, p) in enumerate(self.events)
                  if k == "transfer_started"]
        complete = kinds.index("transfer_complete")
        self.assertEqual(len(starts), 2, "開始が 2 本無い: %s" % self.events)
        self.assertLess(complete, starts[1],
                        "後続の開始が先行の完了より先に出た（同じ行へ相乗りする）: %s"
                        % self.events)

        b.sendto(struct.pack("!HH", OP_ERROR, 0) + b"stop\x00", srvaddr_b)

    def test_the_same_name_can_be_put_again_right_after_a_complete(self):
        """対照: 予約は dally より前に外れるので、置き直す機器を断らない"""
        for payload in (b"first", b"second"):
            c = self._client()
            srvaddr = self._start_upload(c)
            reply = self._send_block(c, srvaddr, 1, payload)
            self.assertEqual(struct.unpack("!H", reply[:2])[0], OP_ACK,
                             "置き直しが断られた: %r" % reply)
        time.sleep(0.3)
        with open(os.path.join(self.root, "cfg.txt"), "rb") as f:
            self.assertEqual(f.read(), b"second")


if __name__ == "__main__":
    unittest.main()
