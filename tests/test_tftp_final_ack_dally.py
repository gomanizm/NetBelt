"""TFTP の最終 ACK が失われても、再送された最終 DATA へ再 ACK できることを確認する。

最終 DATA を受け取ると即座に転送ソケットを閉じていた。最終 ACK が
落ちるとクライアントは最終 DATA を再送するが、応答する相手はもう
おらず（Windows では ICMP Port Unreachable が返る）、サーバは「完了」、
機器は「タイムアウト」と食い違う。実測: 再送した最終 DATA に対する
recv が [WinError 10054] で落ちた。

RFC 1350 のとおり、最終 ACK を送った側はしばらく待ち（dally）、同じ
ブロックの DATA が再び来たら ACK を送り直してから閉じる。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

OP_WRQ, OP_DATA, OP_ACK = 2, 3, 4


def _wrq(filename):
    return struct.pack("!H", OP_WRQ) + filename.encode() + b"\x00octet\x00"


class TftpFinalAckDallyTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-dally-")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda k, ip, p: self.events.append(k))
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.port = self.srv.port

    def _finish_upload(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        c.sendto(_wrq("cfg.txt"), ("127.0.0.1", self.port))
        ack, srvaddr = c.recvfrom(1024)
        self.assertEqual(ack, b"\x00\x04\x00\x00")
        final = struct.pack("!HH", OP_DATA, 1) + b"last block"
        c.sendto(final, srvaddr)
        ack, _ = c.recvfrom(1024)
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 1))
        return c, srvaddr, final

    def test_retransmitted_final_data_is_acked_again(self):
        c, srvaddr, final = self._finish_upload()
        # 最終 ACK が落ちたことにして、クライアントが最終 DATA を再送する
        time.sleep(0.3)
        c.sendto(final, srvaddr)
        try:
            ack, _ = c.recvfrom(1024)
        except OSError as e:
            self.fail("再送した最終 DATA に応答が無い: %r" % (e,))
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 1),
                         "再送された最終 DATA へ ACK が返らない")
        with open(os.path.join(self.root, "cfg.txt"), "rb") as f:
            self.assertEqual(f.read(), b"last block", "再送で内容が二重になった")

    def test_transfer_is_reported_complete_without_waiting_for_the_dally(self):
        """完了通知は最終 ACK の直後に出ること（待ち時間の分だけ遅れない）。"""
        self._finish_upload()
        deadline = time.time() + 0.5
        while "transfer_complete" not in self.events and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn("transfer_complete", self.events,
                      "最終 ACK 後すぐに完了が通知されない: %s" % self.events)

    def test_stop_is_not_held_by_the_dally(self):
        """停止要求が来たら、再送待ちを打ち切ってすぐ抜けること。"""
        self._finish_upload()
        started = time.monotonic()
        self.srv.stop()
        self.assertLess(time.monotonic() - started, 1.5,
                        "再送待ちのぶん stop() が固まっている")


if __name__ == "__main__":
    unittest.main()
