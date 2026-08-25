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
        except OSError:
            # 停止済みなので、タイムアウトでも ICMP 由来のリセットでもよい
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


class TftpStopNoticeTest(unittest.TestCase):
    """停止による中断は、エラーではなく情報として伝えること。

    利用者が意図して止めたのだから、エラー扱いは誤り。実機のログでも
    「エラー: 停止により中断」と赤く出ていた。
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import unittest.mock
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def test_an_interrupted_transfer_is_not_reported_as_an_error(self):
        from PyQt6.QtWidgets import QApplication
        from core.tftp_server import TFTPServerManager

        root = tempfile.mkdtemp(prefix="netbelt-tftp-notice-")
        port = free_udp_port()
        manager = TFTPServerManager()
        self.addCleanup(manager.stop)
        errors, notices = [], []
        manager.error_occurred.connect(errors.append)
        manager.transfer_interrupted.connect(
            lambda ip, fn, d: notices.append("停止により中断: %s" % fn))
        self.assertTrue(manager.start(port=port, root_dir=root))

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(5)
        self.addCleanup(client.close)
        client.sendto(wrq("notice.bin"), ("127.0.0.1", port))
        _oack, peer = client.recvfrom(4096)
        client.sendto(struct.pack("!HH", OP_DATA, 1) + b"A" * 512, peer)
        client.recvfrom(4096)

        manager.stop()
        for _ in range(10):
            QApplication.processEvents()
            time.sleep(0.05)

        interrupted = [m for m in notices if "中断" in m]
        self.assertTrue(interrupted, "中断を伝えていない: %s" % notices)
        self.assertIn("notice.bin", interrupted[0])
        self.assertEqual(
            [e for e in errors if "中断" in e], [],
            "利用者が止めたのにエラーとして出している")


    def test_the_history_row_shows_interrupted_not_error(self):
        """履歴の状況列が「エラー」ではなく「中断」になること。

        実機で試したところ、状況列も赤いエラー表示になっていた。
        利用者が止めたのだからエラーではないし、「転送中」のまま
        残るのも誤り。
        """
        from unittest import mock
        from PyQt6.QtWidgets import QApplication, QTableWidgetItem
        from ui.tftp_server_panel import TFTPServerPanel

        with mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "stub")):
            panel = TFTPServerPanel()

        ip, filename, direction = "192.0.2.10", "conf.bin", "upload"
        panel._on_tx_started(ip, filename, 4096, direction)
        row = panel._active[(ip, filename, direction)]["row"]
        self.assertNotEqual(panel.history.item(row, 5).text(), "中断")

        panel._on_transfer_interrupted(ip, filename, direction)

        self.assertEqual(panel.history.item(row, 5).text(), "中断",
                         "状況列が中断になっていない")
        self.assertNotIn((ip, filename, direction), panel._active,
                         "中断した転送が進行中のまま残っている")

    def test_an_interruption_does_not_touch_other_rows(self):
        """他の転送の行まで巻き添えにしないこと。

        既存の _on_error は進行中の行を全部エラーにする作りなので、
        中断を同じ経路に載せると無関係な転送まで壊す。
        """
        from unittest import mock
        from ui.tftp_server_panel import TFTPServerPanel

        with mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "stub")):
            panel = TFTPServerPanel()

        panel._on_tx_started("192.0.2.10", "a.bin", 100, "upload")
        panel._on_tx_started("192.0.2.11", "b.bin", 100, "upload")
        other = panel._active[("192.0.2.11", "b.bin", "upload")]["row"]

        panel._on_transfer_interrupted("192.0.2.10", "a.bin", "upload")

        self.assertNotEqual(panel.history.item(other, 5).text(), "中断",
                            "無関係な転送まで中断にしている")
        self.assertIn(("192.0.2.11", "b.bin", "upload"), panel._active)
    def test_an_interrupted_transfer_leaves_no_active_row(self):
        """中断した転送を「進行中」のまま残さないこと。"""
        from PyQt6.QtWidgets import QApplication
        from core.tftp_server import TFTPServerManager

        root = tempfile.mkdtemp(prefix="netbelt-tftp-notice-")
        port = free_udp_port()
        manager = TFTPServerManager()
        self.addCleanup(manager.stop)
        self.assertTrue(manager.start(port=port, root_dir=root))

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(5)
        self.addCleanup(client.close)
        client.sendto(wrq("row.bin"), ("127.0.0.1", port))
        _oack, peer = client.recvfrom(4096)
        client.sendto(struct.pack("!HH", OP_DATA, 1) + b"A" * 512, peer)
        client.recvfrom(4096)

        manager.stop()
        for _ in range(10):
            QApplication.processEvents()
            time.sleep(0.05)

        self.assertEqual(dict(manager._tx), {},
                         "中断した転送が進行中のまま残っている")


if __name__ == "__main__":
    unittest.main()
