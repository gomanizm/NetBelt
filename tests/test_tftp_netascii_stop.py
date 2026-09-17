"""netascii の事前サイズ計算が停止要求で打ち切れること。

実測: RRQ に応じる前に _netascii_size() がファイル全体を走査するが、
そのループには停止フラグの判定が無い。停止しても走査中のワーカーは戻らず、
stop() は各ワーカーを join(timeout=_timeout+2) で順に待つことになる。
TFTP は無認証で 0.0.0.0 に待ち受け、max_workers=16 まで同時に走るので、
大きいファイルへ netascii RRQ を並べられると停止が長時間ブロックされる。

読み出し 1 回ぶんの単位で停止を見て打ち切れば、走査の長さに関係なく
すぐ戻れる。
"""
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

OP_RRQ = 1


def _rrq(filename, mode=b"netascii"):
    return struct.pack("!H", OP_RRQ) + filename.encode() + b"\x00" + mode + b"\x00"


class TftpNetasciiStopTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-stop-")
        # 64KiB 単位の読み出しで 16 回ぶん。走査を打ち切らなければ全部読む
        self.path = os.path.join(self.root, "big.txt")
        with open(self.path, "wb") as f:
            f.write(b"a\n" * (512 * 1024))
        self.srv = TFTPServer(port=0, root_dir=self.root)

    def test_size_scan_gives_up_once_stopping(self):
        """停止中なら、走査を最初の読み出しで打ち切ること。"""
        import core.tftp_server as tftp_server

        chunks = []
        real_encode = tftp_server._netascii_encode

        def counting_encode(data):
            chunks.append(len(data))
            return real_encode(data)

        self.srv._stopping = True
        with mock.patch.object(tftp_server, "_netascii_encode", counting_encode):
            # 停止中なので、応答もイベントも出さずに戻るはず
            self.srv._handle_rrq(_rrq("big.txt"), ("127.0.0.1", 50000))

        self.assertLessEqual(len(chunks), 1,
                             "停止したのに netascii の事前走査が最後まで走っている"
                             "（%d 回読んだ）" % len(chunks))

    def test_size_scan_still_counts_the_whole_file_when_running(self):
        """停止していないときは、これまで通り変換後のサイズを数え切ること。"""
        from core.tftp_server import _netascii_size

        expected = os.path.getsize(self.path) + 512 * 1024  # LF が CR LF になるぶん
        self.assertEqual(_netascii_size(self.path), expected)


if __name__ == "__main__":
    unittest.main()
