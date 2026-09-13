"""FTP サーバが、他のサーバと待ち受けの仕組みを共有しないことを検証する。

pyftpdlib の FTPServer は、ioloop を渡さないとプロセス全体で 1 つの
共有インスタンス（IOLoop.instance()）を使う。NetBelt は渡していなかった
ので、複数の FTPServerManager が同じ socket_map と、select() へ渡す同じ
fd のリストを共有していた。

そのため次の 2 つが起きる。

1. 片方を停止すると close_all() が共有 socket_map を空にするので、
   動いているもう片方の待ち受けソケットまで閉じられる。
2. 2 本の待受スレッドが同じ socket_map と fd の一覧を同時に読み書き
   する。pyftpdlib はひとつの ioloop への登録・解除がすべて、それを
   poll しているスレッドから行われる前提で書かれており（どちらも
   素の dict と list で、ロックが無い）、この状態は想定外。

   補足: これを入れた時点では、全体テストを落としていた access
   violation の原因がここだと考えていた。実際の原因は別で
   （tests/test_signal_relay_outlives_receiver.py を参照）、select の
   実行中に別スレッドがその list を縮めても Python 3.12 / Windows 11
   では落ちないことが後の実測で確かめられている。1 の被害は実測で
   再現するので、このテストはそれを見ている。

サーバごとに専用の ioloop を持たせる。1 を直接見れば 2 の共有も
なくなる（共有していなければ他方の fd リストに触れない）。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class FtpServerIsolatedIoloopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patcher = mock.patch("core.firewall.ensure_inbound_allow",
                             return_value=(True, "test stub"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _manager(self):
        from core.ftp_server import FTPServerManager
        m = FTPServerManager()
        root = tempfile.mkdtemp(prefix="netbelt-ftp-iso-")
        self.assertTrue(m.start(port=0, root_dir=root,
                                username="u", password="p"))
        self.addCleanup(m.stop)
        return m

    def _can_log_in(self, port):
        import ftplib
        f = ftplib.FTP()
        try:
            f.connect("127.0.0.1", port, timeout=5)
            f.login("u", "p")
        except (OSError, ftplib.Error):
            return False
        finally:
            try:
                f.close()
            except Exception:
                pass
        return True

    def test_stopping_one_server_leaves_another_one_serving(self):
        a = self._manager()
        b = self._manager()
        time.sleep(0.3)
        self.assertTrue(self._can_log_in(b.port), "前提: B は起動直後に使える")

        a.stop()
        time.sleep(0.3)

        self.assertTrue(
            self._can_log_in(b.port),
            "A を止めたら B の待ち受けまで閉じられた（ioloop を共有している）")

    def test_each_server_has_its_own_ioloop(self):
        a = self._manager()
        b = self._manager()

        self.assertIsNot(a._server.ioloop, b._server.ioloop,
                         "2 つのサーバが同じ ioloop を使っている")

    def test_a_stopped_server_releases_only_its_own_port(self):
        from core.sockets import set_exclusive_bind
        a = self._manager()
        b = self._manager()
        port_a, port_b = a.port, b.port

        a.stop()

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        set_exclusive_bind(s)
        try:
            s.bind(("0.0.0.0", port_a))
        except OSError as e:
            self.fail("停止した側のポート %d が解放されない: %r" % (port_a, e))

        t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(t.close)
        set_exclusive_bind(t)
        with self.assertRaises(OSError,
                               msg="動いている側のポート %d まで解放された" % port_b):
            t.bind(("0.0.0.0", port_b))


if __name__ == "__main__":
    unittest.main()
