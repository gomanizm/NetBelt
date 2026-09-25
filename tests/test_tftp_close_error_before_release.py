"""TFTP の close() 失敗の通知を、保存先の予約を外すより先に出すことを検証する。

実測（基準 81664d2）: 中断で終わった転送 A の finally で close() が OSError(28)
を投げると、その失敗の通知（protocol_error「アップロード失敗（保存できません）:
...」）が _release_target の後に出ていた（src/core/tftp_server.py:493-501）。
予約が外れてから通知が出るまでの間に、同じ相手・同じ名前の次の WRQ（B）が
確立できるので、A の失敗通知が B の台帳の行を掴む。
  - TFTPServerManager の台帳は B の行（{'count': 1, 'done': False,
    'row': 'shown'}）を count 0 まで下げて pop し、row=='shown' なので
    closing=True で protocol_event を出す（src/core/tftp_server.py:935-955）。
  - src/ui/tftp_server_panel.py:302 の _on_protocol_event はその行を「エラー」で
    確定させるため、実際には成功する B（516 バイト保存）が「エラー」の行として
    残り、A の失敗理由が B の名前で表示される。
成功経路の同じ順序違い（完了より先に予約を外していた件）は
tests/test_tftp_complete_before_release.py で直っており、close_error の通知だけが
この順序のまま残っていた。

隙間は release -> xs.close() -> emit の数マイクロ秒なので、自然状態ではまず
踏めない。_release_target に gate を入れて広げると必ず踏む（上記の既存テストと
同じ手法）。

直し方: finally の close_error の通知を _release_target より前へ移す。通知の側で
例外が出ても予約外しと xs.close() へ必ず到達させるため、通知は try/finally の
try 側に置く（この到達保証は tests/test_tftp_wrq_close_failure_release.py が
作ったもので、崩さない）。
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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

OP_DATA, OP_ACK, OP_ERROR = 3, 4, 5


def _wrq(filename, mode=b"octet"):
    return b"\x00\x02" + filename.encode() + b"\x00" + mode + b"\x00"


class _FailsOnClose(io.FileIO):
    """close() で「ディスク満杯」を起こすファイル（既存テストと同じ）。"""

    def close(self):
        if not self.closed:
            super().close()
            raise OSError(28, "No space left on device")


class _TftpClientMixin:
    """クライアント側の手順（両方のテストで使う）"""

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

    def _gate_release(self, srv):
        """最初の _release_target の後で止める gate を仕掛け、隙間を広げる"""
        released = threading.Event()
        gate = threading.Event()
        real_release = srv._release_target
        first = {"done": False}

        def gated_release(target):
            real_release(target)
            if not first["done"]:
                first["done"] = True
                released.set()
                gate.wait(5.0)

        srv._release_target = gated_release
        self.addCleanup(setattr, srv, "_release_target", real_release)
        self.addCleanup(gate.set)
        return released, gate

    def _fail_the_first_write(self):
        """最初の書き込み用 open だけ close() で失敗させる（後続 B は成功させる）"""
        import core.tftp_server as mod
        real_open = open
        opened = {"n": 0}

        def failing_open(path, mode="r", *a, **kw):
            if "w" in mode:
                opened["n"] += 1
                if opened["n"] == 1:
                    return _FailsOnClose(path, "w")
            return real_open(path, mode, *a, **kw)

        patcher = mock.patch.object(mod, "open", failing_open, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _abort_a_and_establish_b(self, srv):
        """A を close() 失敗で中断させ、予約が外れた隙間で B を確立させる"""
        released, gate = self._gate_release(srv)
        self._fail_the_first_write()

        a = self._client()
        srvaddr_a = self._start_upload(a)
        self.assertEqual(self._send_block(a, srvaddr_a, 1, b"x" * 512)[:2],
                         b"\x00\x04", "非最終ブロックに ACK が返らない")
        # 相手が打ち切った（停止・タイムアウトと同じ中断の経路）
        a.sendto(struct.pack("!HH", OP_ERROR, 0) + b"user abort\x00", srvaddr_a)
        self.assertTrue(released.wait(5.0), "予約が外れない")

        b = self._client()
        srvaddr_b = self._start_upload(b)
        self.assertEqual(self._send_block(b, srvaddr_b, 1, b"z" * 512)[:2],
                         b"\x00\x04", "後続が確立できない")
        return b, srvaddr_b, gate


class TftpCloseErrorBeforeReleaseTest(_TftpClientMixin, unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-close-order-")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda k, ip, p: self.events.append((k, p)))
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.port = self.srv.port

    def _wait_for_workers(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.srv._workers:
                time.sleep(0.05)
                return
            time.sleep(0.02)
        self.fail("転送スレッドが終わらない")

    # --- 本題 ------------------------------------------------------------

    def test_the_close_failure_is_announced_before_the_target_is_released(self):
        b, srvaddr_b, gate = self._abort_a_and_establish_b(self.srv)
        gate.set()
        time.sleep(0.3)

        starts = [i for i, (k, _) in enumerate(self.events)
                  if k == "transfer_started"]
        failures = [i for i, (k, p) in enumerate(self.events)
                    if k == "protocol_error" and "保存できません" in p[1]]
        self.assertEqual(len(starts), 2, "開始が 2 本無い: %s" % self.events)
        self.assertTrue(failures,
                        "保存できなかったことを知らせていない: %s" % self.events)
        self.assertLess(failures[0], starts[1],
                        "A の close() 失敗の通知が B の開始より後に出た"
                        "（B の台帳の行を掴む）: %s" % self.events)
        b.sendto(struct.pack("!HH", OP_ERROR, 0) + b"stop\x00", srvaddr_b)

    def test_the_following_upload_still_finishes_normally(self):
        """隙間で確立した B は、A の失敗に巻き込まれず最後まで通ること"""
        b, srvaddr_b, gate = self._abort_a_and_establish_b(self.srv)
        gate.set()
        time.sleep(0.3)
        self.assertEqual(self._send_block(b, srvaddr_b, 2, b"tail")[:2],
                         b"\x00\x04", "後続が続けられない")
        time.sleep(0.3)

        completes = [p for k, p in self.events if k == "transfer_complete"]
        self.assertEqual([p[1] for p in completes], [516],
                         "後続の完了が無い: %s" % self.events)
        with open(os.path.join(self.root, "cfg.txt"), "rb") as f:
            self.assertEqual(f.read(), b"z" * 512 + b"tail")

    def test_the_target_is_released_even_if_the_announcement_raises(self):
        """通知で例外が出ても予約は外れること（既存の到達保証を崩さない）"""
        thread_errors = []
        hook = mock.patch.object(
            threading, "excepthook",
            lambda args, log=thread_errors: log.append(args.exc_type.__name__))
        hook.start()
        self.addCleanup(hook.stop)

        def raising_on_event(kind, ip, payload):
            self.events.append((kind, payload))
            if kind == "protocol_error" and "保存できません" in payload[1]:
                raise RuntimeError("GUI 側で例外")

        self.srv.on_event = raising_on_event
        self._fail_the_first_write()

        a = self._client()
        srvaddr = self._start_upload(a)
        self.assertEqual(self._send_block(a, srvaddr, 1, b"x" * 512)[:2],
                         b"\x00\x04")
        a.sendto(struct.pack("!HH", OP_ERROR, 0) + b"user abort\x00", srvaddr)
        self._wait_for_workers()

        self.assertEqual(self.srv._wrq_targets, set(),
                         "保存先の予約が残っている: %s" % self.srv._wrq_targets)
        self.assertEqual(thread_errors, ["RuntimeError"],
                         "通知の例外だけが上がるはず: %s" % thread_errors)

    def test_a_close_failure_without_a_following_upload_is_unchanged(self):
        """対照: 後続が無ければこれまでどおり 1 件知らせて予約も外れる"""
        self._fail_the_first_write()
        a = self._client()
        srvaddr = self._start_upload(a)
        self.assertEqual(self._send_block(a, srvaddr, 1, b"x" * 512)[:2],
                         b"\x00\x04")
        a.sendto(struct.pack("!HH", OP_ERROR, 0) + b"user abort\x00", srvaddr)
        self._wait_for_workers()

        kinds = [k for k, _ in self.events]
        self.assertIn("interrupted", kinds)
        self.assertEqual(
            len([p for k, p in self.events
                 if k == "protocol_error" and "保存できません" in p[1]]), 1,
            "保存できなかった通知が 1 件でない: %s" % self.events)
        self.assertEqual(self.srv._wrq_targets, set())


class TftpCloseErrorLedgerTest(_TftpClientMixin, unittest.TestCase):
    """台帳（TFTPServerManager._tx）の側から見る"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtCore import Qt
        from core.tftp_server import TFTPServerManager
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-close-ledger-")
        self.signals = []
        self.m = TFTPServerManager()
        self.addCleanup(self.m.stop)
        for name in ("transfer_started", "transfer_complete",
                     "transfer_interrupted", "protocol_event"):
            getattr(self.m, name).connect(self._recorder(name),
                                          Qt.ConnectionType.DirectConnection)
        self.assertTrue(self.m.start(port=0, root_dir=self.root))
        self.port = self.m._srv.port

    def _recorder(self, name):
        # DirectConnection なので、発行したスレッドでそのまま記録される
        return lambda *a: self.signals.append((name,) + a)

    def _row(self):
        with self.m._tx_lock:
            return dict(self.m._tx.get(("127.0.0.1", "cfg.txt", "upload")) or {})

    def test_the_close_failure_does_not_take_the_following_rows_entry(self):
        b, srvaddr_b, gate = self._abort_a_and_establish_b(self.m._srv)
        before = self._row()
        self.assertEqual(before, {"count": 1, "done": False, "row": "shown"},
                         "後続が台帳に載っていない: %s" % self.signals)

        gate.set()
        time.sleep(0.5)
        self.assertEqual(
            self._row(), before,
            "A の close() 失敗が B の台帳の行を降ろした（B の行が「エラー」で"
            "確定し、A の理由が B の名前で表示される）: %s" % self.signals)

        kinds = [s[0] for s in self.signals]
        self.assertIn("protocol_event", kinds,
                      "保存できなかったことを知らせていない: %s" % self.signals)
        starts = [i for i, s in enumerate(self.signals)
                  if s[0] == "transfer_started"]
        self.assertEqual(len(starts), 2, "開始が 2 本無い: %s" % self.signals)
        self.assertLess(kinds.index("protocol_event"), starts[1],
                        "A の失敗通知が B の開始より後に出た: %s" % self.signals)
        b.sendto(struct.pack("!HH", OP_ERROR, 0) + b"stop\x00", srvaddr_b)
