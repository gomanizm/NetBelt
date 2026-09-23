"""srv-03: TFTP の停止待ちが、止まっている転送の本数に比例して延びる件。

何が起きていたか（実測、基準 470c538。127.0.0.1 のみ）: 別名への
アップロード 4 本を、保存先の close() の中で止めた（共有フォルダの遅延・
切断を模した）まま TFTPServer.stop() を呼ぶと、戻るまで 17.0 秒かかった。
stop() は転送スレッドを 1 本ずつ join(timeout=self._timeout + 2) で待つので、
既定（2 秒）では 1 本につき 4 秒ずつ積み上がる（上限 16 本なら約 64 秒）。
パネルは GUI スレッドから同期で stop() を呼ぶので、その間画面が固まる。

どう直したか: 全転送の join に共通の期限（今の 1 本ぶん = timeout + 2 秒）を
使い、合計の待ちがそれを超えないようにした。期限を過ぎて生き残った転送は
これまでどおり覚えておき、消えるまで次の起動を断る（stop() は False を返す）。
"""
import builtins
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

OP_WRQ, OP_DATA = 2, 3
STUCK = 4               # close() で止める転送の本数
TRANSFER_TIMEOUT = 1.0  # 1 本ぶんの待ちは TRANSFER_TIMEOUT + 2 秒


class _StuckOnClose:
    """close() が合図まで戻らない書き込み用ファイル。"""

    def __init__(self, handle, gate, entered):
        self._handle = handle
        self._gate = gate
        self._entered = entered

    def write(self, data):
        return self._handle.write(data)

    def close(self):
        self._entered.release()
        self._gate.wait(60)
        self._handle.close()


class TftpStopWaitBoundedTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.gate = threading.Event()
        self.entered = threading.Semaphore(0)
        real_open = builtins.open
        gate, entered = self.gate, self.entered

        def fake_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            return _StuckOnClose(handle, gate, entered) if "w" in mode else handle

        patcher = mock.patch("core.tftp_server.open", fake_open, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = TFTPServer(
            port=0, root_dir=tempfile.mkdtemp(prefix="netbelt-tftp-stopwait-"))
        self.server._timeout = TRANSFER_TIMEOUT
        self.server.start()
        self.addCleanup(self.server.stop)
        # 後始末は後から足したものが先に走る。止めた close() を先に通してから
        # 停止させる（逆だと後始末の停止がもう 1 本ぶん待つ）
        self.addCleanup(self.gate.set)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()

    def _leave_uploads_stuck_in_close(self, count):
        for i in range(count):
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(5)
            self.clients.append(client)
            client.sendto(struct.pack("!H", OP_WRQ) + b"stuck%d.cfg\x00octet\x00" % i,
                          ("127.0.0.1", self.server.port))
            _ack, tid = client.recvfrom(1024)
            # 1 ブロックだけの転送。最終ブロックなので close() で止まる
            client.sendto(struct.pack("!HH", OP_DATA, 1) + b"config", tid)
        for _ in range(count):
            self.assertTrue(self.entered.acquire(timeout=10),
                            "転送が close() まで進んでいない（前提が崩れている）")

    def test_stop_waits_once_in_total_not_once_per_transfer(self):
        self._leave_uploads_stuck_in_close(STUCK)

        began = time.monotonic()
        self.server.stop()
        elapsed = time.monotonic() - began

        per_transfer = TRANSFER_TIMEOUT + 2
        # 待受スレッドの待ち（通常 1 秒以内）の余裕を足しても、
        # 1 本ぶんの待ちを大きく超えないこと（本数倍なら 12 秒を超える）
        self.assertLess(elapsed, per_transfer + 3.0,
                        "停止に %.1f 秒かかった（1 本ぶんは %.1f 秒）"
                        % (elapsed, per_transfer))

    def test_survivors_past_the_deadline_are_still_remembered(self):
        """期限を過ぎた生き残りを覚え、消えるまで「終わっていない」と返すこと。"""
        self._leave_uploads_stuck_in_close(STUCK)

        finished = self.server.stop()

        self.assertIs(finished, False, "生き残りがいるのに停止しきれたと返している")
        self.assertEqual(len(self.server.unfinished_workers()), STUCK)
        self.gate.set()
        deadline = time.monotonic() + 10
        while self.server.unfinished_workers() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.server.unfinished_workers(), [],
                         "生き残りが終わっても残ったままになっている")


if __name__ == "__main__":
    unittest.main()
