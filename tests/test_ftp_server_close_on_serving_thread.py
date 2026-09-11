"""FTP サーバの停止が、待受スレッド自身にソケットを閉じさせることを検証する。

stop() は呼び出し元（GUI スレッド）から close_all() を呼んでいた。
pyftpdlib 2.2.0 の Select ioloop は select.select(self._r, ...) に素の
list を渡しており、close_all() はその list から fd を remove する。
待受スレッドが select の中でその list を走査している最中に別スレッドが
remove すると、CPython は縮んだ配列の外を読む。

実測: 全体テストの tests/test_ftp_server.py::FtpServerTest::
test_manager_coalesces_multiple_started_same_key の実行中に
"Windows fatal exception: access violation" が出て、スタックの先頭は
pyftpdlib/ioloop.py:474 の poll、落ちたのは直前のテストが残した
serve_forever スレッドだった。アプリでも、FTP サーバを停止する操作は
GUI スレッドから stop() を呼ぶので同じ経路を踏む。

閉じるのは ioloop を回しているスレッド自身に任せる。停止の待ち合わせ
（上限つき join）は tests/test_ftp_server_stop_joins.py が見ている。
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, "src")


class FtpServerCloseOnServingThreadTest(unittest.TestCase):
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
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-close-")

    def test_only_the_serving_thread_closes_the_sockets(self):
        from pyftpdlib.servers import FTPServer
        from core.ftp_server import FTPServerManager

        closed_on = []
        original = FTPServer.close_all

        def recording_close_all(server, *args, **kwargs):
            closed_on.append(threading.current_thread())
            return original(server, *args, **kwargs)

        with mock.patch.object(FTPServer, "close_all", recording_close_all):
            m = FTPServerManager()
            self.assertTrue(m.start(port=0, root_dir=self.root,
                                    username="u", password="p"))
            thread = m._thread
            self.assertTrue(thread.is_alive(), "前提: 起動直後はスレッドが生きている")

            m.stop()

            self.assertFalse(thread.is_alive(),
                             "前提: stop() は待受スレッドの終了を待つ")

        self.assertTrue(closed_on, "close_all が呼ばれていない")
        self.assertEqual(
            set(closed_on), {thread},
            "待受スレッド以外から close_all が呼ばれた: %s（待受スレッド: %s）"
            % (closed_on, thread))

    def test_the_port_is_free_again_after_stop(self):
        """閉じるのを待受スレッドに任せても、stop() の時点で解放済みであること。"""
        import socket
        from core.ftp_server import FTPServerManager
        from core.sockets import set_exclusive_bind

        m = FTPServerManager()
        self.assertTrue(m.start(port=0, root_dir=self.root,
                                username="u", password="p"))
        port = m.port

        m.stop()

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        set_exclusive_bind(s)
        try:
            s.bind(("0.0.0.0", port))
        except OSError as e:
            self.fail("stop() のあともポート %d が掴まれたまま: %r" % (port, e))


if __name__ == "__main__":
    unittest.main()
