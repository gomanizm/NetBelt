"""FTP サーバの停止が、待受スレッドの終了まで待つことを検証する。

FTPServerManager.stop() は close_all() を呼んで _server を None にした
直後に戻っていた。serve_forever は daemon スレッドで動いており、
close_all() は socket_map を空にするだけなので、スレッドが実際に
抜けるのは次に poll() から戻ったあとになる。つまり stop() のあとも
しばらくスレッドが生きている（pyftpdlib 2.2.0 の loop() は
`while socket_map:` で回り、poll(timeout=1.0) を抜けてから判定する）。

その間にマネージャ（QObject）が破棄されると、まだ ioloop の中にいる
スレッドからの emit が解放済みのオブジェクトへ届きうる。アプリでも
「FTP サーバを停止した直後にパネルやアプリを閉じる」で同じ経路を踏む。

注: これを書いた時点では、テストスイートの間欠 segfault の根本原因が
これだと考えていた。実際の根本原因は SNMPManager のシグナル中継で
（tests/test_signal_relay_outlives_receiver.py を参照）、そちらを直す
までスイートは落ち続けた。待たずに戻ること自体は正しくないので、
この検証は残している。

停止は、スレッドが抜けるのを（上限つきで）待ってから戻ること。

退出の遅さは環境で揺れる（数 µs のこともある）ので、テストでは
待受スレッドの終了処理に明示的な遅れを入れて、待たない実装が必ず
落ちるようにしてある。遅れを入れる先は close_all()。ソケットを閉じる
のは待受スレッド自身の役目になったため（pyftpdlib は ioloop への
登録・解除をそれを poll しているスレッドが行う前提で書かれている。
tests/test_ftp_server_close_on_serving_thread.py を参照）、居残りはこの
終了処理の中で起きる。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 待受スレッドが抜けきるまでの遅れの模擬。実物は最長 POLL_INTERVAL_SECONDS
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

    def _started(self, exit_delay=EXIT_DELAY):
        """起動済みのマネージャと、その待受スレッドを返す。

        exit_delay 秒だけ終了処理に居座る待受スレッドにする。
        """
        from pyftpdlib.servers import FTPServer
        from core.ftp_server import FTPServerManager
        if exit_delay:
            original = FTPServer.close_all

            def close_slowly(server, *args, **kwargs):
                time.sleep(exit_delay)
                return original(server, *args, **kwargs)

            patcher = mock.patch.object(FTPServer, "close_all", close_slowly)
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
        # 上限（3 秒）より十分長く終了処理に居座らせて、抜けない状況を作る
        m, thread = self._started(exit_delay=30.0)
        started = time.monotonic()

        m.stop()

        self.assertLess(time.monotonic() - started, 10.0,
                        "スレッドが抜けないと stop() が戻らない")
        # 後片付け: 居座りが明けるまで待つ（デーモンスレッドなので放置でも
        # 落ちはしないが、次のテストへソケットを持ち越さない）
        thread.join(timeout=60)


if __name__ == "__main__":
    unittest.main()
