"""Syslog サーバの停止が、受信スレッドのソケットを横から閉じないことを検証する。

停止処理は stop_event を立てたあと、受信スレッドがブロックしている
ソケットを呼び出し側のスレッドから close() していた。Windows では
recvfrom / accept でブロック中のハンドルを他スレッドが解放すると、
そのブロック中の呼び出しがアクセス違反で落ちる。全体テストで数回に1度
プロセスごと落ちていたのがこれで、スタックは recvfrom を指していた。

UDP・TCP どちらのソケットにも settimeout(1.0) があるので、
stop_event だけで1秒以内にループは抜け、自分の finally でソケットを閉じる。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")


def free_port(kind):
    s = socket.socket(socket.AF_INET,
                      socket.SOCK_DGRAM if kind == "UDP" else socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ClosingSpy:
    """close() を誰が呼んだかだけ記録する薄い包み。

    受信スレッドには元のソケットが引数で渡っているので、これを
    _servers の側だけに差し込めば、停止処理が閉じたかどうかが分かる。
    """

    def __init__(self, sock):
        self._sock = sock
        self.closed_by = []

    def close(self):
        self.closed_by.append(threading.current_thread().name)
        # 実際には閉じない（閉じると本来の不具合を再現してしまう）

    def __getattr__(self, name):
        return getattr(self._sock, name)


class SyslogShutdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import unittest.mock
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _receiver(self):
        from core.syslog_receiver import SyslogReceiver
        r = SyslogReceiver()
        self.addCleanup(r.stop)
        return r

    def test_the_stopping_thread_does_not_close_the_socket(self):
        """停止側のスレッドがソケットを閉じないこと。"""
        for proto in ("UDP", "TCP"):
            with self.subTest(proto=proto):
                r = self._receiver()
                self.assertTrue(r.start_protocol(proto, port=free_port(proto)))
                spy = _ClosingSpy(r._servers[proto]["socket"])
                r._servers[proto]["socket"] = spy

                r.stop_protocol(proto)

                self.assertEqual(
                    spy.closed_by, [],
                    "停止側のスレッドがソケットを閉じた: %s" % spy.closed_by)

    def test_the_receiving_thread_finishes_on_its_own(self):
        """ソケットを閉じなくても、スレッドは stop_event で終わること。"""
        for proto in ("UDP", "TCP"):
            with self.subTest(proto=proto):
                r = self._receiver()
                self.assertTrue(r.start_protocol(proto, port=free_port(proto)))
                thread = r._servers[proto]["thread"]

                r.stop_protocol(proto)

                self.assertFalse(thread.is_alive(),
                                 "停止後も受信スレッドが生きている")

    def test_the_port_is_released_so_it_can_be_reused(self):
        """スレッドが自分で閉じるので、同じポートを再び使えること。

        停止側で閉じるのをやめた結果ポートが握られたままになると、
        再起動できなくなる。そこを固定する。
        """
        r = self._receiver()
        port = free_port("UDP")
        for i in range(5):
            with self.subTest(cycle=i):
                self.assertTrue(r.start_protocol("UDP", port=port),
                                "%d 回目の起動に失敗" % (i + 1))
                r.stop_protocol("UDP")

    def test_messages_still_arrive_before_the_stop(self):
        """停止経路を変えても、通常の受信は壊れていないこと。"""
        from PyQt6.QtWidgets import QApplication
        r = self._receiver()
        port = free_port("UDP")
        self.assertTrue(r.start_protocol("UDP", port=port))

        got = []
        r.message_received.connect(got.append)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"<14>test message", ("127.0.0.1", port))
        s.close()

        deadline = time.time() + 5
        while time.time() < deadline and not got:
            QApplication.processEvents()
            time.sleep(0.02)
        self.assertTrue(got, "Syslog を受信できない")

        r.stop_protocol("UDP")


if __name__ == "__main__":
    unittest.main()
