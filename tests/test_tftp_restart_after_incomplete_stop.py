"""srv-01 の回帰テスト: 停止しきれていないのに TFTP を再起動できてしまう件。

何が起きていたか（実測、基準 aa38a2b）: TFTPServer.stop() は転送スレッドを
join(timeout=self._timeout + 2) で待つが、戻り値も is_alive() も見ていない。
共有フォルダ相手の close() のように期限内に終わらない転送が残っていても、
TFTPServerManager.stop() は _srv を捨てて is_running を False にするだけで、
警告もログも出さなかった。

  1) 旧サーバで WRQ が 1 件走り、close() の中で止まる
  2) stop() が 5.0 秒で復帰。生き残りの転送スレッドが 1 本残り、
     その _wrq_targets には保存先が入ったままになる
  3) 同じ root/port で新サーバを起動できてしまう。新サーバの
     _wrq_targets は空なので、同名の WRQ をそのまま受理する
  4) 新しい内容を書いた後で旧 close() が復帰し、握っていた
     バッファを書き戻す -> ファイルの中身が b'OLD-CONTENT-NEW-CONTENT'
     のように混ざる

機器の config を受け取る道具なので、黙って壊れた内容が残るのは許容できない。
同種の防御は FTP 側にはあった（ftp_server.PREVIOUS_STOP_INCOMPLETE_MESSAGE と
_await_previous_thread）が、TFTP には無かった。

利用者の決定（2026-09-23）: 再起動を断る。停止の期限を過ぎても生き残って
いる転送スレッドがある間は、次の start() を FTP と同じ形で
「前回の停止が完了していません」と断る。待たないので画面は固まらない。
生き残りが消えたら、これまでどおり起動できる。

どう直したか: TFTPServer.stop() は期限内に終わらなかったワーカーを覚え、
unfinished_workers() で今の生存分を返す（stop() 自身も終わったかを返す）。
TFTPServerManager.stop() はその旧サーバの参照を捨てず、start() は生き残りが
ある間 PREVIOUS_STOP_INCOMPLETE_MESSAGE を出して False を返す。
"""
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

OP_WRQ = 2
OP_DATA = 3


def free_udp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _BlockingClose:
    """close() が合図を受けるまで戻らないファイル。

    共有フォルダの切断・輻輳で close() が長く掛かる状況を模す。
    書き出しは close() の中で起きるので、実機でもここが詰まる。
    """

    def __init__(self, handle, gate, entered):
        self._handle = handle
        self._gate = gate
        self._entered = entered

    def write(self, data):
        return self._handle.write(data)

    def close(self):
        self._entered.set()
        self._gate.wait(30)
        self._handle.close()


class TftpRestartAfterIncompleteStopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import core.tftp_server as tftp_server
        self.root = tempfile.mkdtemp(prefix="netbelt-tftp-restart-")
        self.port = free_udp_port()
        self.gate = threading.Event()        # close() を通す合図
        self.closing = threading.Event()     # close() に入った合図
        self.slow = {"on": True}
        real_open = open
        gate, closing, slow = self.gate, self.closing, self.slow

        def patched_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if slow["on"] and "w" in mode:
                return _BlockingClose(handle, gate, closing)
            return handle

        patcher = mock.patch.object(tftp_server, "open", patched_open,
                                    create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        firewall = mock.patch("core.firewall.ensure_inbound_allow",
                              return_value=(True, "test stub"))
        firewall.start()
        self.addCleanup(firewall.stop)

    def tearDown(self):
        self.gate.set()   # 生き残りを解放してから後片付けへ入る

    def _manager(self):
        from core.tftp_server import TFTPServerManager
        manager = TFTPServerManager()
        self.addCleanup(manager.stop)
        return manager

    def _upload_stuck_in_close(self, manager):
        """WRQ を 1 件通し、その転送が close() の中で止まるまで待つ。"""
        self.assertTrue(manager.start(port=self.port, root_dir=self.root))

        def send():
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(5)
            try:
                client.sendto(
                    struct.pack("!H", OP_WRQ) + b"config.cfg\0octet\0",
                    ("127.0.0.1", self.port))
                _first, peer = client.recvfrom(4096)
                client.sendto(struct.pack("!HH", OP_DATA, 1) + b"OLD-CONTENT",
                              peer)
                client.recvfrom(4096)   # 最終 ACK は close の後なので来ない
            except OSError:
                pass
            finally:
                client.close()

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        self.assertTrue(self.closing.wait(10),
                        "転送が close() まで進んでいない（前提が崩れている）")

    def test_start_is_refused_while_the_previous_transfer_survives(self):
        """生き残りがいる間の起動を断り、理由を伝えること。"""
        manager = self._manager()
        self._upload_stuck_in_close(manager)

        manager.stop()
        self.assertFalse(manager.is_running)

        errors = []
        manager.error_occurred.connect(errors.append)
        self.slow["on"] = False
        began = time.monotonic()
        started = manager.start(port=self.port, root_dir=self.root)
        elapsed = time.monotonic() - began

        self.assertFalse(started, "生き残りがいるのに起動を受け付けている")
        from core.tftp_server import PREVIOUS_STOP_INCOMPLETE_MESSAGE
        self.assertIn(PREVIOUS_STOP_INCOMPLETE_MESSAGE, errors,
                      "断った理由を伝えていない: %r" % (errors,))
        self.assertLess(elapsed, 3.0,
                        "断るのに待っている（画面が固まる）: %.1f秒" % elapsed)

    def test_start_works_again_once_the_survivor_is_gone(self):
        """生き残りが消えたら、これまでどおり起動できること。"""
        manager = self._manager()
        self._upload_stuck_in_close(manager)
        manager.stop()
        self.assertFalse(manager.start(port=self.port, root_dir=self.root),
                         "生き残りがいるのに起動を受け付けている")

        self.slow["on"] = False
        self.gate.set()                 # 旧転送の close() を通す
        deadline = time.time() + 15
        while time.time() < deadline:
            if manager.start(port=self.port, root_dir=self.root):
                break
            time.sleep(0.1)

        self.assertTrue(manager.is_running,
                        "生き残りが消えても起動できないままになっている")

    def test_a_clean_stop_still_allows_a_restart(self):
        """生き残りのいない普通の停止は、これまでどおりすぐ起動できること。"""
        self.slow["on"] = False
        manager = self._manager()
        self.assertTrue(manager.start(port=self.port, root_dir=self.root))
        manager.stop()

        self.assertTrue(manager.start(port=self.port, root_dir=self.root),
                        "普通に停止した後に起動できない")


if __name__ == "__main__":
    unittest.main()
