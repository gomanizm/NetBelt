"""FTP サーバの停止が、待受スレッドの終了まで待つことを検証する。

FTPServerManager.stop() は close_all() を呼んで _server を None にした
直後に戻っていた。serve_forever は daemon スレッドで動いており、
close_all() は socket_map を空にするだけなので、スレッドが実際に
抜けるのは次に poll() から戻ったあとになる。つまり stop() のあとも
しばらくスレッドが生きている（pyftpdlib 2.2.0 の loop() は
`while socket_map:` で回り、poll(timeout=1.0) を抜けてから判定する）。

その間にマネージャ（QObject）が破棄されると、まだ ioloop の中にいる
スレッドからの emit が解放済みのオブジェクトへ届き、access violation で
プロセスごと落ちる。テストスイートの間欠 segfault の根本原因で、
変更前のツリーにダミーテストを 1 枚足すだけで再現した。アプリでも
「FTP サーバを停止した直後にパネルやアプリを閉じる」で同じ経路を踏む。

停止は、スレッドが抜けるのを（上限つきで）待ってから戻ること。

退出の遅さは環境で揺れる（数 µs のこともある）ので、テストでは
serve_forever の戻りに明示的な遅れを入れて、待たない実装が必ず
落ちるようにしてある。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

# poll(timeout) から戻るまでの遅れの模擬。実物は最長 1 秒
EXIT_DELAY = 0.5


class FtpServerStopJoinsTest(unittest.TestCase):
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
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-join-")

    def _started(self, slow_exit=True):
        """起動済みのマネージャと、その待受スレッドを返す。"""
        from pyftpdlib.servers import FTPServer
        from core.ftp_server import FTPServerManager
        if slow_exit:
            original = FTPServer.serve_forever

            def serve_then_linger(server, *args, **kwargs):
                original(server, *args, **kwargs)
                time.sleep(EXIT_DELAY)

            patcher = mock.patch.object(FTPServer, "serve_forever",
                                        serve_then_linger)
            patcher.start()
            self.addCleanup(patcher.stop)
        m = FTPServerManager()
        self.assertTrue(m.start(port=0, root_dir=self.root,
                                username="u", password="p"))
        thread = m._thread
        self.assertTrue(thread.is_alive(), "前提: 起動直後はスレッドが生きている")
        return m, thread

    def test_stop_returns_only_after_the_serving_thread_has_exited(self):
        """stop() から戻った時点で、待受スレッドが生きていないこと。"""
        m, thread = self._started()

        m.stop()

        self.assertFalse(thread.is_alive(),
                         "stop() が待受スレッドの終了を待たずに戻っている")

    def test_stop_does_not_hang_if_the_thread_will_not_exit(self):
        """スレッドが抜けない場合でも、上限で諦めて戻ること。

        待つのは正しいが、無期限に待つと今度は停止ボタンでアプリが
        固まる。上限を設ける。
        """
        m, thread = self._started(slow_exit=False)
        # close_all を無効化して、スレッドが抜けない状況を作る
        real_close_all = m._server.close_all
        m._server.close_all = lambda: None
        started = time.monotonic()

        m.stop()

        self.assertLess(time.monotonic() - started, 10.0,
                        "スレッドが抜けないと stop() が戻らない")
        # 後片付け: 本物の close_all でスレッドを抜けさせる
        real_close_all()
        thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
