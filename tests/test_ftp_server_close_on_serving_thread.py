"""FTP サーバの停止が、待受スレッド自身にソケットを閉じさせることを検証する。

stop() は呼び出し元（GUI スレッド）から close_all() を呼んでいた。
pyftpdlib 2.2.0 は、ひとつの ioloop に対する登録・解除はすべて
その ioloop を poll しているスレッドから行われる前提で書かれている
（socket_map も fd の一覧も素の dict と list で、ロックが無い）。
停止だけが別スレッドから入ると、その前提が崩れる。実測でも、停止の
直後に待受スレッドが WinError 10038（ソケットでないものへの操作）を
投げていた。

閉じるのは ioloop を回しているスレッド自身に任せる。停止の待ち合わせ
（上限つき join）は tests/test_ftp_server_stop_joins.py が見ている。

経緯の補足: この形にしたときは、全体テストを落としていた access
violation の原因がここだと考えていた。実際の原因は別で、
tests/test_signal_relay_outlives_receiver.py が押さえている。
select の実行中に別スレッドがその list を縮めても落ちないことは、
3.8 億回の試行で確かめられている。この形自体は pyftpdlib の前提に
沿っているので残している。
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
    def test_start_tells_the_user_when_the_previous_stop_timed_out(self):
        """join がタイムアウトした場合の境界。

        ソケットを閉じるのが待受スレッド自身になったので、join が上限で
        諦めるとポートを掴んだまま stop() が戻る。その状態で同じポートへ
        start() すると、以前は bind 失敗（または SO_REUSEADDR で前のサーバ
        へ横取り）になり、利用者には原因が分からなかった。
        前回の停止が終わっていないことを知らせ、抜けたあとは再起動できる
        こと。
        """
        from pyftpdlib.servers import FTPServer
        from core.ftp_server import FTPServerManager

        release = threading.Event()
        self.addCleanup(release.set)
        original = FTPServer.close_all

        def close_when_released(server, *args, **kwargs):
            release.wait(60.0)
            return original(server, *args, **kwargs)

        patcher = mock.patch.object(FTPServer, "close_all", close_when_released)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 待ち時間を詰める。停止要求に気づくのは POLL_INTERVAL_SECONDS 周期
        with mock.patch.object(FTPServerManager, "STOP_TIMEOUT_SECONDS", 0.5):
            m = FTPServerManager()
            errors = []
            m.error_occurred.connect(errors.append)
            self.assertTrue(m.start(port=0, root_dir=self.root,
                                    username="u", password="p"))
            port = m.port
            thread = m._thread

            m.stop()
            self.assertTrue(
                thread.is_alive(),
                "前提: 待受スレッドが抜けない状況を作れていない（join が成功した）")

            # ポートはまだ掴まれたまま。同じポートでの再起動は失敗するが、
            # その理由が利用者に伝わること
            self.assertFalse(
                m.start(port=port, root_dir=self.root,
                        username="u", password="p"),
                "前回の待受スレッドが残っているのに起動を受け付けた")
            self.assertTrue(
                any("前回の停止" in e for e in errors),
                "前回の停止が完了していないことが知らされていない: %r" % (errors,))
            self.assertFalse(m.is_running)

            release.set()
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "前提: 居座りを解除できていない")

            # 待受スレッドが抜けたあとは、同じポートで起動できる
            errors.clear()
            self.assertTrue(
                m.start(port=port, root_dir=self.root,
                        username="u", password="p"),
                "待受スレッドが抜けたあとも同じポートで起動できない: %r" % (errors,))
            m.stop()


if __name__ == "__main__":
    unittest.main()
