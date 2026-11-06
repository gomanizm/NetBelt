# -*- coding: utf-8 -*-
"""TFTP が Windows で別の実体に化ける名前を、ルート拘束の前に断ることを確かめる。

_safe_join は realpath と commonpath でルート配下を確かめていたが、NUL などの
予約デバイス名、`backup.cfg:evil` の代替データストリーム、末尾のドット・空白は
「ルート配下のパス」として通っていた。WRQ 'NUL' は中身が捨てられるのに完了と
表示され、'backup.cfg:evil' は既存のバックアップに一覧へ出ないストリームを足し、
'running-config. ' は 'running-config' に化けていた。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from core.tftp_server import TFTPServer, _safe_join, OP_ERROR  # noqa: E402


def _req(op, filename):
    return struct.pack("!H", op) + filename.encode() + b"\x00octet\x00"


class SafeJoinRefusesUnsafeNamesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-names-")

    def test_unsafe_names_raise_value_error(self):
        for name in ("NUL", "nul", "COM1", "sub/CON", "NUL.txt", "backup.cfg:evil",
                     "running-config. ", "cfg.", "/LPT1"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    _safe_join(self.root, name)

    def test_plain_names_still_pass(self):
        for name in ("running-config", "sub/normal.cfg", "/c9300.bin", "設定.txt"):
            with self.subTest(name=name):
                got = _safe_join(self.root, name)
                self.assertTrue(got.startswith(os.path.realpath(self.root)))


class TftpRefusesUnsafeNamesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-names-")
        with open(os.path.join(self.root, "backup.cfg"), "wb") as f:
            f.write(b"REAL")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda *a: self.events.append(a))
        self.srv.start()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()

    def _send(self, op, name):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        c.sendto(_req(op, name), ("127.0.0.1", self.port))
        reply, _ = c.recvfrom(1024)
        return reply

    def test_wrq_to_unsafe_names_is_an_access_violation(self):
        for name in ("NUL", "backup.cfg:evil", "running-config. "):
            with self.subTest(name=name):
                reply = self._send(2, name)
                self.assertEqual(struct.unpack("!HH", reply[:4]), (OP_ERROR, 2))
                self.assertIn(b"Access violation", reply)
        time.sleep(0.3)
        self.assertEqual(sorted(os.listdir(self.root)), ["backup.cfg"])
        with open(os.path.join(self.root, "backup.cfg"), "rb") as f:
            self.assertEqual(f.read(), b"REAL")
        refused = [e for e in self.events if e[0] == "protocol_error"]
        self.assertEqual(len(refused), 3)
        for _kind, _ip, (fn, reason, direction) in refused:
            self.assertEqual(reason, "パス外への書き込みを拒否")
            self.assertEqual(direction, "upload")

    def test_rrq_of_a_device_name_is_an_access_violation(self):
        reply = self._send(1, "NUL")
        self.assertEqual(struct.unpack("!HH", reply[:4]), (OP_ERROR, 2))


if __name__ == "__main__":
    unittest.main()
