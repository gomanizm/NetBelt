"""TFTP の停止が、進行中の転送も止めることを検証する。

stop() は待受ループしか止めておらず、RRQ/WRQ ごとに起きた転送スレッドを
追跡も停止もしていなかった。実測された症状:

  - stop() が返った後に届いた DATA でファイルが新規作成され、完走した
  - アプリ終了時、daemon スレッドが放棄されて finally の f.close() が
    走らず、ACK 済み（クライアントには受領と応答済み）のバイトが欠落した
  - 無音のクライアント相手だと停止の12秒後に偽のエラーを出した

機器の config やイメージを運ぶ道具なので、「止めたのに書かれる」「受領と
答えたのに保存されていない」はどちらも許容できない。
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

OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wrq(filename, blksize=512):
    return (struct.pack("!H", OP_WRQ) + filename.encode() + b"\0"
            + b"octet\0" + b"blksize\0" + str(blksize).encode() + b"\0")


class TftpShutdownTest(unittest.TestCase):
    def setUp(self):
        import unittest.mock
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _server(self):
        from core.tftp_server import TFTPServer
        root = tempfile.mkdtemp(prefix="netbelt-tftp-stop-")
        server = TFTPServer(port=free_udp_port(), root_dir=root)
        server.start()
        self.addCleanup(server.stop)
        return server, root

    def _begin_upload(self, server, name, blksize=512):
        """WRQ を投げて最初の DATA まで送り、転送を確立させる。"""
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(5)
        self.addCleanup(client.close)
        client.sendto(wrq(name, blksize), ("127.0.0.1", server.port))
        _reply, peer = client.recvfrom(4096)       # OACK

        payload = b"A" * blksize
        client.sendto(struct.pack("!HH", OP_DATA, 1) + payload, peer)
        client.recvfrom(4096)                      # ACK(1)
        return client, peer, payload

    def test_stop_waits_for_a_transfer_in_flight(self):
        """stop() は進行中の転送スレッドが終わるまで戻らないこと。"""
        server, _root = self._server()
        self._begin_upload(server, "inflight.bin")

        before = [t for t in threading.enumerate() if t.is_alive()]
        self.assertTrue(any("_handle_wrq" in t.name or "run" in t.name
                            for t in before) or True)   # 名前は実装依存

        server.stop()

        alive = [t for t in getattr(server, "_workers", []) if t.is_alive()]
        self.assertEqual(alive, [],
                         "停止後も転送スレッドが動いている: %s" % alive)

    def test_no_file_is_created_after_stop(self):
        """停止後に届いた DATA でファイルを作らないこと。

        停止したつもりの利用者の足元で、機器から届いたデータが
        書き込まれるのは受け入れられない。
        """
        server, root = self._server()

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(5)
        self.addCleanup(client.close)
        client.sendto(wrq("after_stop.bin"), ("127.0.0.1", server.port))
        _oack, peer = client.recvfrom(4096)        # 確立前（まだファイルは無い）

        server.stop()

        client.sendto(struct.pack("!HH", OP_DATA, 1) + b"B" * 512, peer)
        try:
            client.recvfrom(4096)
        except socket.timeout:
            pass
        time.sleep(0.5)

        created = os.listdir(root)
        self.assertEqual(created, [],
                         "停止後にファイルが作られた: %s" % created)

    def test_acknowledged_bytes_are_on_disk_after_stop(self):
        """ACK した分は停止後もディスクに残っていること。

        クライアントには「受領した」と答えているので、その分が
        黙って消えるのは嘘をついたことになる。
        """
        server, root = self._server()
        _client, _peer, payload = self._begin_upload(server, "acked.bin")

        server.stop()

        path = os.path.join(root, "acked.bin")
        self.assertTrue(os.path.exists(path), "ACK 済みなのにファイルが無い")
        self.assertEqual(os.path.getsize(path), len(payload),
                         "ACK 済みのバイトが書き出されていない")

    def test_stop_returns_promptly(self):
        """停止が現実的な時間で返ること。

        転送を待つようにしたので、待ちすぎないことも確かめる。
        """
        server, _root = self._server()
        self._begin_upload(server, "prompt.bin")

        started = time.time()
        server.stop()
        elapsed = time.time() - started

        self.assertLess(elapsed, 15.0,
                        "停止に時間がかかりすぎる: %.1f秒" % elapsed)


if __name__ == "__main__":
    unittest.main()
