"""同じ保存先への TFTP アップロードが重なったら、後から来た方を断ること。

各 WRQ のワーカーが独立に open(target, "wb") していて、保存先ごとの排他が
無かった。実測（scratchpad\\cx5b-srv\\srv01_tftp_concurrent_wrq.py）:
A に 512 バイトを受領させて待たせ、B に 1024 バイトを完了させ、そのあと
A を 1 バイトで完了させると、両方に最終 ACK と transfer_complete が出る
のに、残ったファイルは A の 513 バイトと B の末尾 511 バイトが混ざった
ものだった。機器から見ると 2 台とも「成功」なのに、置いたはずの
コンフィグが別の機器のものと混ざる。

利用者の決定（2026-09-20）に沿った直し方: 同じ保存先（同じ実パス。
Windows なので大文字小文字や短縮名の違いも同じとみなす）への書き込みが
進行中なら、後から来た方を TFTP の ERROR で断り、その保存先には何も
書かない。断ったことは画面のログにも残す。予約はワーカーが終わるとき
（成功・失敗・停止・相手の ERROR・タイムアウト）に必ず外すので、
打ち切った直後のやり直しは受け付けられる。読み出し（RRQ）は対象外。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

OP_WRQ, OP_DATA, OP_ACK, OP_ERROR = 2, 3, 4, 5
BLOCK = 512


def _wrq(filename):
    return struct.pack("!H", OP_WRQ) + filename.encode() + b"\x00octet\x00"


def _data(block, payload):
    return struct.pack("!HH", OP_DATA, block) + payload


class TftpConcurrentUploadRefusedTest(unittest.TestCase):
    def setUp(self):
        from core.tftp_server import TFTPServer
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-same-target-")
        self.events = []
        self.srv = TFTPServer(port=0, root_dir=self.root,
                              on_event=lambda k, ip, p: self.events.append((k, p)))
        self.srv.start()
        self.addCleanup(self.srv.stop)

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        return c

    def _establish(self, filename, payload):
        """WRQ を出し、最初の DATA を ACK させて転送を確立させる。(client, tid)"""
        c = self._client()
        c.sendto(_wrq(filename), ("127.0.0.1", self.srv.port))
        ack, peer = c.recvfrom(1024)
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 0))
        c.sendto(_data(1, payload), peer)
        ack, _ = c.recvfrom(1024)
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 1))
        return c, peer

    def _finish(self, c, peer, block, payload):
        c.sendto(_data(block, payload), peer)
        ack, _ = c.recvfrom(1024)
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, block))

    def _refused(self, filename, payload=b"B" * BLOCK):
        """WRQ を出して最初の DATA を送り、返ってきたパケットを返す"""
        c = self._client()
        c.sendto(_wrq(filename), ("127.0.0.1", self.srv.port))
        ack, peer = c.recvfrom(1024)
        self.assertEqual(ack, struct.pack("!HH", OP_ACK, 0))
        c.sendto(_data(1, payload), peer)
        return c.recvfrom(1024)[0]

    def _read(self, filename):
        with open(os.path.join(self.root, filename), "rb") as f:
            return f.read()

    def _wait_until(self, cond, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    def test_the_second_upload_of_the_same_file_is_refused(self):
        a, a_tid = self._establish("cfg.txt", b"A" * BLOCK)

        reply = self._refused("cfg.txt")
        self.assertEqual(struct.unpack("!H", reply[:2])[0], OP_ERROR,
                         "後から来た同じ保存先への書き込みが断られていない")

        self._finish(a, a_tid, 2, b"A")          # 先行は最後まで通る
        self.assertTrue(self._wait_until(
            lambda: "transfer_complete" in [k for k, _ in self.events]))
        self.assertEqual(self._read("cfg.txt"), b"A" * BLOCK + b"A",
                         "断ったはずの転送の中身が混ざった")
        completes = [p for k, p in self.events if k == "transfer_complete"]
        self.assertEqual(len(completes), 1,
                         "断った側にも完了を出している: %r" % (completes,))

    def test_the_refusal_is_reported_so_the_user_can_see_it(self):
        a, a_tid = self._establish("cfg.txt", b"A" * BLOCK)
        self._refused("cfg.txt")

        notices = [p for k, p in self.events if k == "protocol_error"]
        self.assertTrue(notices, "断ったことが通知されない")
        self.assertIn("cfg.txt", notices[-1][1])
        # 進行中の転送の行を閉じてしまわないよう、ファイル名は空で渡す
        self.assertEqual(notices[-1][0], "")
        self._finish(a, a_tid, 2, b"A")

    def test_a_retry_right_after_an_abort_is_accepted(self):
        a, a_tid = self._establish("cfg.txt", b"A" * BLOCK)
        a.sendto(struct.pack("!HH", OP_ERROR, 0) + b"aborted\x00", a_tid)
        self.assertTrue(self._wait_until(
            lambda: "interrupted" in [k for k, _ in self.events]))

        b, b_tid = self._establish("cfg.txt", b"B" * BLOCK)
        self._finish(b, b_tid, 2, b"BB")
        self.assertTrue(self._wait_until(
            lambda: "transfer_complete" in [k for k, _ in self.events]))
        self.assertEqual(self._read("cfg.txt"), b"B" * BLOCK + b"BB")

    def test_another_upload_of_the_same_file_after_a_success_is_accepted(self):
        a, a_tid = self._establish("cfg.txt", b"A" * BLOCK)
        self._finish(a, a_tid, 2, b"A")
        self.assertTrue(self._wait_until(
            lambda: "transfer_complete" in [k for k, _ in self.events]))

        # 最終 ACK の後の待ち（_dally）の間も、次の書き込みを断らない
        b, b_tid = self._establish("cfg.txt", b"B" * BLOCK)
        self._finish(b, b_tid, 2, b"BB")
        self.assertTrue(self._wait_until(
            lambda: len([k for k, _ in self.events if k == "transfer_complete"]) == 2))
        self.assertEqual(self._read("cfg.txt"), b"B" * BLOCK + b"BB")

    def test_a_different_name_for_the_same_file_is_refused_too(self):
        a, a_tid = self._establish("cfg.txt", b"A" * BLOCK)

        reply = self._refused("CFG.TXT")     # Windows では同じ保存先
        self.assertEqual(struct.unpack("!H", reply[:2])[0], OP_ERROR,
                         "大文字小文字だけ違う名前がすり抜けた")
        self._finish(a, a_tid, 2, b"A")

    def test_uploads_to_different_files_are_not_affected(self):
        a, a_tid = self._establish("one.txt", b"1" * BLOCK)
        b, b_tid = self._establish("two.txt", b"2" * BLOCK)
        self._finish(a, a_tid, 2, b"1")
        self._finish(b, b_tid, 2, b"2")

        self.assertTrue(self._wait_until(
            lambda: len([k for k, _ in self.events if k == "transfer_complete"]) == 2))
        self.assertEqual(self._read("one.txt"), b"1" * BLOCK + b"1")
        self.assertEqual(self._read("two.txt"), b"2" * BLOCK + b"2")


class TftpRefusalDoesNotCloseTheRunningRowTest(unittest.TestCase):
    """断りの通知が、進行中の転送の行を閉じてしまわないこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_manager_keeps_the_running_transfer_in_its_ledger(self):
        from core.tftp_server import TFTPServerManager
        m = TFTPServerManager()
        seen = []
        m.protocol_event.connect(
            lambda ip, fn, reason, d: seen.append((fn, reason)))
        m.transfer_complete.connect(
            lambda ip, fn, done, total, d: seen.append(("complete", fn)))

        m._on_event("transfer_started", "192.0.2.10", ("cfg.txt", 512, "upload"))
        m._on_event("protocol_error", "192.0.2.10",
                    ("", "同じファイルへの書き込み中のため断りました: cfg.txt",
                     "upload"))
        m._on_event("transfer_complete", "192.0.2.10",
                    ("cfg.txt", 513, 513, "upload"))
        self.app.processEvents()

        self.assertIn(("complete", "cfg.txt"), seen,
                      "断りの通知が進行中の転送を台帳から落とした: %r" % (seen,))

    def test_the_panel_logs_the_refusal_without_closing_the_row(self):
        from unittest import mock
        from ui.tftp_server_panel import TFTPServerPanel
        with mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "stub")):
            cfg = mock.Mock()
            cfg.get_server_settings.return_value = {}
            panel = TFTPServerPanel(config_manager=cfg)

        panel._on_tx_started("192.0.2.10", "cfg.txt", 512, "upload")
        row = panel._active[("192.0.2.10", "cfg.txt", "upload")]["row"]
        panel._on_protocol_event(
            "192.0.2.10", "", "同じファイルへの書き込み中のため断りました: cfg.txt",
            "upload")

        self.assertIn("断りました", panel.log_text.toPlainText())
        self.assertNotEqual(panel.history.item(row, 5).text(), "エラー",
                            "進行中の転送の行が断りの通知で閉じられた")
        self.assertIn(("192.0.2.10", "cfg.txt", "upload"), panel._active)


if __name__ == "__main__":
    unittest.main()
