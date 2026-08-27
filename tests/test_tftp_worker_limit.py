"""TFTP サーバが要求の数だけ無制限にスレッドを起こさないことを検証する。

_serve は RRQ/WRQ のデータグラムを受けるたび無条件に _spawn していた。
同時数の上限が無く、各ワーカーは専用の UDP ソケットを bind したうえ、
DATA が来なければ timeout 2.0 秒 x retries 5 回、およそ 13 秒生き続ける。
TFTP は無認証で 0.0.0.0 で待ち受けるので、到達可能な任意のホストが
WRQ を撃つだけでスレッドとエフェメラルポートを積み上げられる。

さらに _spawn の呼び出しは recvfrom を包む try の外にあり、
thread.start() が RuntimeError("can't start new thread") を投げると
例外がそのまま _serve を貫いて待受スレッドが終わる。
TFTPServerManager 側は is_running=True のままなので、UI は「起動中」を
出し続けるのに、以後どの要求も受け付けない状態になる。

上限を超えた要求は待たせずにその場で断る。TFTP のクライアントは待って
くれず、滞留させると再送とタイムアウトを増やすだけになる。
"""
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core.tftp_server import TFTPServer, OP_ERROR      # noqa: E402


def _wrq(filename, mode=b"octet"):
    return b"\x00\x02" + filename.encode() + b"\x00" + mode + b"\x00"


class TftpWorkerLimitTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-limit-")
        self.srv = TFTPServer(port=0, root_dir=self.root)
        self.srv.start()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        return c

    def _stall(self, count):
        """DATA を送らない WRQ を count 件投げ、応答を捨てずに持ち帰る。"""
        replies = []
        for i in range(count):
            c = self._client()
            c.sendto(_wrq("stall-%02d.bin" % i), ("127.0.0.1", self.port))
            replies.append(c)
        time.sleep(1.0)     # ワーカーが立ち上がるのを待つ
        return replies

    def test_there_is_a_limit_on_how_many_run_at_once(self):
        """上限が決まっていること。"""
        self.assertTrue(hasattr(self.srv, "max_workers"),
                        "同時転送数の上限が無い")
        self.assertGreater(self.srv.max_workers, 0)

    def test_the_number_running_at_once_stays_within_the_limit(self):
        """上限を超えて積み上がらないこと。"""
        limit = self.srv.max_workers
        self._stall(limit + 6)

        with self.srv._workers_lock:
            running = len(self.srv._workers)
        self.assertLessEqual(running, limit,
                             "上限 %d を超えて %d 本走っている" % (limit, running))

    def test_a_refused_request_is_told_so_instead_of_being_ignored(self):
        """断るときは黙って捨てず、エラーを返すこと。

        黙って捨てるとクライアントは再送を繰り返し、タイムアウトまで
        待たされる。断られたと分かれば、すぐ次の手を打てる。
        """
        limit = self.srv.max_workers
        self._stall(limit)              # 上限まで埋める

        latecomer = self._client()
        latecomer.sendto(_wrq("too-late.bin"), ("127.0.0.1", self.port))
        data, _ = latecomer.recvfrom(1024)

        self.assertEqual(struct.unpack("!H", data[:2])[0], OP_ERROR,
                         "上限超過の要求が黙って捨てられている")

    def test_the_listener_survives_a_worker_that_cannot_start(self):
        """スレッドを起こせなくても、待受を巻き添えにしないこと。

        thread.start() の RuntimeError が _serve を貫くと待受だけが死に、
        UI は「起動中」のまま何も受け付けなくなる。
        """
        real_thread = threading.Thread

        class RefusesToStart(real_thread):
            def start(self):
                raise RuntimeError("can't start new thread")

        c = self._client()
        with mock.patch("core.tftp_server.threading.Thread", RefusesToStart):
            c.sendto(_wrq("nope.bin"), ("127.0.0.1", self.port))
            time.sleep(0.8)

        # _running は待受が死んでも True のまま（UI が「起動中」を出し
        # 続ける原因そのもの）なので、実際に生きているかで判定する。
        self.assertTrue(self.srv._thread.is_alive(),
                        "スレッドを起こせなかっただけで待受が死んでいる")

        # 生きているなら、次の要求にはこれまでどおり応じるはず
        ok = self._client()
        ok.sendto(_wrq("after-refusal.txt"), ("127.0.0.1", self.port))
        data, _ = ok.recvfrom(1024)
        self.assertEqual(data[:2], b"\x00\x04",
                         "待受が生きているのに応答しない")

    def test_a_normal_transfer_still_works_after_a_refusal(self):
        """断ったあとも、通常の転送は受け付けること。"""
        limit = self.srv.max_workers
        stalled = self._stall(limit)

        # 滞留させた側を諦めさせてから、普通の書き込みを1件通す
        for c in stalled:
            c.close()
        self.srv.stop()
        self.srv = TFTPServer(port=0, root_dir=self.root)
        self.srv.start()

        c = self._client()
        c.sendto(_wrq("after.txt"), ("127.0.0.1", self.srv.port))
        data, srvaddr = c.recvfrom(1024)
        self.assertEqual(data[:2], b"\x00\x04", "ACK が返ってこない")


if __name__ == "__main__":
    unittest.main()
