"""TFTP の最終 ACK 後の待ち（dally）が、クライアントの再送より先に終わらないこと。

最終 ACK を送ったあと待つのは、交渉済み timeout（未交渉なら既定 2 秒）の
1 回分だけで、再 ACK を返しても締切は延びなかった。最終 ACK が落ち、
クライアントがそれより長い間隔（例: 3 秒）で最終 DATA を再送すると、
転送ソケットは閉じたあとで、ファイルは保存済みなのに機器側は失敗になる。
実測（最終 ACK を落としたことにして再送を遅らせた）: 未交渉では 1.5 秒後は
再 ACK、2.5・3.0・5.5 秒後は ConnectionResetError（WinError 10054）。
timeout=5 を交渉しても 5.5 秒後は 10054。どの場合もファイルは保存済み。

直し方: 待つ長さを、クライアントの再送を 2 回ほど受けられる timeout の
3 倍（最低 6 秒、長い timeout では上限あり、ただし従来の timeout 1 回分より
短くしない）に延ばし、再 ACK を返すたびに締切も延ばす。完了通知は従来どおり
待つ前に出し、停止要求にはすぐ応じる。
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


class TftpDallyWindowTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-dally-window-")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda k, ip, p: self.events.append(k))
        self.srv.start()
        self.addCleanup(self.srv.stop)

    def _finish_upload(self, name):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        c.sendto(_wrq(name), ("127.0.0.1", self.srv.port))
        ack, srvaddr = c.recvfrom(1024)
        self.assertEqual(ack, b"\x00\x04\x00\x00")
        final = struct.pack("!HH", OP_DATA, 1) + b"last block"
        c.sendto(final, srvaddr)
        ack, _ = c.recvfrom(1024)
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 1))
        return c, srvaddr, final

    def _resend_after(self, c, srvaddr, final, delay, what):
        time.sleep(delay)
        c.sendto(final, srvaddr)
        c.settimeout(1.0)
        try:
            ack, _ = c.recvfrom(1024)
        except OSError as e:
            self.fail("%s: 再送した最終 DATA に応答が無い: %r" % (what, e))
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 1),
                         "%s: 再送された最終 DATA へ ACK が返らない" % what)

    def test_final_data_resent_after_longer_intervals_is_acked_again(self):
        c, srvaddr, final = self._finish_upload("cfg.txt")
        # 最終 ACK が落ち、クライアントが既定の timeout（2 秒）より長い
        # 3 秒の間隔で最終 DATA を再送する
        self._resend_after(c, srvaddr, final, 3.0, "1 回目の再送（3 秒後）")
        # その再 ACK も落ち、さらに 4 秒後にもう一度再送する（最終 ACK から
        # 7 秒後。再 ACK で締切が延びていなければ閉じたあと）
        self._resend_after(c, srvaddr, final, 4.0, "2 回目の再送（さらに 4 秒後）")
        with open(os.path.join(self.root, "cfg.txt"), "rb") as f:
            self.assertEqual(f.read(), b"last block", "再送で内容が二重になった")
        self.assertEqual(self.events.count("transfer_complete"), 1)

        # 延びた待ちの最中でも、停止要求にはすぐ応じる
        started = time.monotonic()
        self.srv.stop()
        self.assertLess(time.monotonic() - started, 1.5,
                        "再送待ちのぶん stop() が固まっている")


if __name__ == "__main__":
    unittest.main()
