"""TFTP のアップロードで、ファイルを閉じられなかったら成功と言わないことを検証する。

_handle_wrq は最終 DATA を受け取ると、その場で ACK を返し、transfer_complete を
出してから finally で f.close() していた。バッファ付きファイルは close() で
最後の書き出しをするので、ディスク満杯・共有フォルダ切断・書き込み権限の
消失はここで初めて例外になる。その時点で機器には最終 ACK、画面には
「完了」が届いており、close() は例外処理の外なので protocol_error も
出せない。機器は「送れた」と思って次へ進み、設定ファイルは欠けている。

最終ブロックはファイルを閉じてから応答する。閉じられなければ ACK ではなく
ERROR（コード 3: Disk full or allocation exceeded）を返し、protocol_error を
出す。
"""
import io
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

OP_DATA, OP_ACK, OP_ERROR = 3, 4, 5


def _wrq(filename, mode=b"octet"):
    return b"\x00\x02" + filename.encode() + b"\x00" + mode + b"\x00"


class _FailsOnClose(io.FileIO):
    """close() で「ディスク満杯」を起こすファイル。"""

    def close(self):
        if not self.closed:
            super().close()
            raise OSError(28, "No space left on device")


class TftpCloseFailureTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-close-")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda k, ip, p: self.events.append((k, p)))
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.port = self.srv.port

    def _upload(self, payload):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        c.sendto(_wrq("cfg.txt"), ("127.0.0.1", self.port))
        ack0, srvaddr = c.recvfrom(1024)
        self.assertEqual(ack0[:2], b"\x00\x04")
        c.sendto(b"\x00\x03" + struct.pack("!H", 1) + payload, srvaddr)
        reply, _ = c.recvfrom(1024)
        time.sleep(0.3)
        return reply

    def _kinds(self):
        return [k for k, _ in self.events]

    def test_a_close_failure_is_reported_as_an_error_not_as_success(self):
        """close() が失敗したら、完了ではなくエラーとして扱うこと。"""
        import core.tftp_server as mod
        real_open = open

        def failing_open(path, mode="r", *a, **kw):
            if "w" in mode:
                return _FailsOnClose(path, "w")
            return real_open(path, mode, *a, **kw)

        with mock.patch.object(mod, "open", failing_open, create=True):
            reply = self._upload(b"hostname R1")

        self.assertNotIn("transfer_complete", self._kinds(),
                         "書き込みが失敗したのに完了を通知している: %s" % self.events)
        self.assertIn("protocol_error", self._kinds(),
                      "失敗を通知していない: %s" % self.events)

    def test_the_client_gets_an_error_packet_instead_of_the_final_ack(self):
        """機器側にも「成功」の ACK を返さないこと。"""
        import core.tftp_server as mod
        real_open = open

        def failing_open(path, mode="r", *a, **kw):
            if "w" in mode:
                return _FailsOnClose(path, "w")
            return real_open(path, mode, *a, **kw)

        with mock.patch.object(mod, "open", failing_open, create=True):
            reply = self._upload(b"hostname R1")

        self.assertEqual(struct.unpack("!H", reply[:2])[0], OP_ERROR,
                         "最終ブロックに ACK を返している: %r" % reply)
        self.assertEqual(struct.unpack("!H", reply[2:4])[0], 3,
                         "エラーコードが Disk full (3) ではない: %r" % reply)

    def test_a_normal_upload_still_gets_its_final_ack_and_completes(self):
        """対照: 正常時はこれまでどおり ACK と完了。"""
        reply = self._upload(b"hostname R1")

        self.assertEqual(reply[:2], b"\x00\x04")
        self.assertEqual(struct.unpack("!H", reply[2:4])[0], 1)
        self.assertIn("transfer_complete", self._kinds())
        with open(os.path.join(self.root, "cfg.txt"), "rb") as f:
            self.assertEqual(f.read(), b"hostname R1")


if __name__ == "__main__":
    unittest.main()
