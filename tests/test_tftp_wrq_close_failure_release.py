"""TFTP の中断時に close() が失敗しても、保存先の予約を残さないことを検証する。

実測（基準 16101ef）: 512 バイトの非最終 DATA を 1 本受領させた後にクライアントから
ERROR を送り、close() が OSError(28) を投げるファイル（既存
tests/test_tftp_close_failure.py と同じ _FailsOnClose）を使うと、_handle_wrq の
finally にある f.close() がそのまま例外を上げ、後続の 2 文へ到達しなかった。
  - _release_target(target) を飛ばす -> srv._wrq_targets に保存先が残ったまま。
    同名の WRQ を再試行すると最初の DATA への応答が
    b'\\x00\\x05\\x00\\x00' + b'File busy: another upload is writing it\\x00' になり、
    イベントは ('protocol_error', ('', '他の転送が書き込み中のため断りました: cfg.txt',
    'upload'))。ディスクが復旧しても、サーバを止めるまで同じ保存先へ二度と書けない。
  - xs.close() を飛ばす -> 転送用 UDP ソケット（TID）が閉じられない。
  - 転送スレッドが未処理例外で死ぬ（OSError: [Errno 28] が run() を抜ける）。
RRQ 側（src/core/tftp_server.py:700 付近）は with open(...) が try の内側なので
OSError が except に拾われ、xs.close() も必ず走る。壊れていたのは WRQ の finally だけ。
既存 tests/test_tftp_close_failure.py は最終 DATA の専用 close 経路しか見ていない。

直し方: finally の f.close() を try/except OSError で包み、_release_target(target) と
xs.close() へ必ず到達させる。閉じられなかったことは最終 DATA 経路と同じ
protocol_error（「アップロード失敗（保存できません）: …」）で 1 件知らせる。
利用者が「中断」だけを見て保存できたと誤解しないようにするため。
"""
import io
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

OP_DATA, OP_ACK, OP_ERROR = 3, 4, 5


def _wrq(filename, mode=b"octet"):
    return b"\x00\x02" + filename.encode() + b"\x00" + mode + b"\x00"


class _FailsOnClose(io.FileIO):
    """close() で「ディスク満杯」を起こすファイル（既存テストと同じ）。"""

    def close(self):
        if not self.closed:
            super().close()
            raise OSError(28, "No space left on device")


class TftpWrqCloseFailureReleaseTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-wrq-close-")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda k, ip, p: self.events.append((k, p)))
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.port = self.srv.port
        # 転送スレッドを抜けた例外を数える（run() は try/finally だけなので、
        # 例外はスレッドの未処理例外として出る）
        self.thread_errors = []
        hook = mock.patch.object(
            threading, "excepthook",
            lambda args, log=self.thread_errors: log.append(args.exc_type.__name__))
        hook.start()
        self.addCleanup(hook.stop)

    # --- クライアント側の手順 ------------------------------------------

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        return c

    def _start_upload(self, c, filename="cfg.txt"):
        """WRQ を出して ACK0 を受け、転送用アドレスを返す"""
        c.sendto(_wrq(filename), ("127.0.0.1", self.port))
        ack0, srvaddr = c.recvfrom(1024)
        self.assertEqual(ack0[:2], b"\x00\x04", "ACK0 が返らない: %r" % ack0)
        return srvaddr

    def _send_block(self, c, srvaddr, block, payload):
        c.sendto(b"\x00\x03" + struct.pack("!H", block) + payload, srvaddr)
        reply, _ = c.recvfrom(1024)
        return reply

    def _abort_after_one_block(self, filename="cfg.txt"):
        """非最終 DATA を 1 本渡してからクライアント側で ERROR を送る"""
        import core.tftp_server as mod
        real_open = open

        def failing_open(path, mode="r", *a, **kw):
            if "w" in mode:
                return _FailsOnClose(path, "w")
            return real_open(path, mode, *a, **kw)

        c = self._client()
        with mock.patch.object(mod, "open", failing_open, create=True):
            srvaddr = self._start_upload(c, filename)
            reply = self._send_block(c, srvaddr, 1, b"x" * 512)
            self.assertEqual(reply[:2], b"\x00\x04",
                             "非最終ブロックに ACK が返らない: %r" % reply)
            # 相手が打ち切った（停止・タイムアウトと同じ中断の経路）
            c.sendto(struct.pack("!HH", OP_ERROR, 0) + b"user abort\x00", srvaddr)
            self._wait_for_workers()

    def _wait_for_workers(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.srv._workers:
                time.sleep(0.05)
                return
            time.sleep(0.02)
        self.fail("転送スレッドが終わらない")

    def _kinds(self):
        return [k for k, _ in self.events]

    # --- 本題 ------------------------------------------------------------

    def test_an_aborted_upload_releases_its_target_when_close_fails(self):
        self._abort_after_one_block()
        self.assertEqual(self.srv._wrq_targets, set(),
                         "保存先の予約が残っている: %s" % self.srv._wrq_targets)

    def test_the_same_name_can_be_uploaded_again_after_a_close_failure(self):
        self._abort_after_one_block()

        # ディスクが復旧した後（open を差し替えない）の置き直し
        c = self._client()
        srvaddr = self._start_upload(c)
        reply = self._send_block(c, srvaddr, 1, b"hostname R1")
        self.assertEqual(struct.unpack("!H", reply[:2])[0], OP_ACK,
                         "再試行が拒否された: %r" % reply)
        time.sleep(0.3)
        with open(os.path.join(self.root, "cfg.txt"), "rb") as f:
            self.assertEqual(f.read(), b"hostname R1")

    def test_the_close_failure_is_reported_so_it_is_not_read_as_a_plain_abort(self):
        self._abort_after_one_block()
        failures = [p for k, p in self.events
                    if k == "protocol_error" and "保存できません" in p[1]]
        self.assertTrue(failures,
                        "保存できなかったことを知らせていない: %s" % self.events)

    def test_the_transfer_thread_does_not_die_on_the_close_failure(self):
        self._abort_after_one_block()
        self.assertEqual(self.thread_errors, [],
                         "転送スレッドが未処理例外で落ちた: %s" % self.thread_errors)

    def test_a_plain_abort_without_a_close_failure_still_releases_the_target(self):
        """対照: close() が成功する場合はこれまでどおり"""
        c = self._client()
        srvaddr = self._start_upload(c)
        reply = self._send_block(c, srvaddr, 1, b"y" * 512)
        self.assertEqual(reply[:2], b"\x00\x04")
        c.sendto(struct.pack("!HH", OP_ERROR, 0) + b"user abort\x00", srvaddr)
        self._wait_for_workers()

        self.assertEqual(self.srv._wrq_targets, set())
        self.assertIn("interrupted", self._kinds())
        self.assertFalse([p for k, p in self.events
                          if k == "protocol_error" and "保存できません" in p[1]],
                         "正常に閉じたのに失敗を通知している: %s" % self.events)


if __name__ == "__main__":
    unittest.main()
