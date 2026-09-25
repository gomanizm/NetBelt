"""connect() が動き出す前の dispose() が、後から成立する Telnet 接続を取り消すことを検証する。

何が起きていたか（基準 f4cad23 で実測）。MainWindow はタブを閉じるとき
_dispose_connection で dispose() を呼び、続けて deleteLater まで行う
（src/ui/main_window.py:1459/1477）。接続そのものは別スレッドで始まる
（同 1078）ので、そのスレッドが connect() の本体へ入る前に dispose() が
着地することがある。SSH とシリアルにはこの順序を塞ぐ _disposed の印が
あるのに、TelnetConnection だけ持っていなかった。

実測（.../scratchpad/cx7a-conn/v04_telnet_dispose.py、127.0.0.1 の受け口）:
  connect() の戻り値      : True
  is_connected            : True
  socket                  : <socket ... raddr=('127.0.0.1', <port>)>
  読み取りスレッド生存    : True
  受け口が受け付けた数    : 1
  出たシグナル            : ['connected']
deleteLater まで処理した後では connect() が
「RuntimeError: wrapped C/C++ object of type TelnetConnection has been
deleted」で抜けるが、その時点で TCP は既に張られており、閉じる経路を
持つ者が誰もいない（機器の vty 枠を掴んだまま残る）。

どう直したか。SSH / シリアルと同じ _disposed の印を足し、connect() の
入口で True なら何もせず False を返すようにした。disconnect() は
繋ぎ直しのために印を戻す（SSH / シリアルと同じ）。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")

from core.telnet_connection import TelnetConnection       # noqa: E402


class _Listener:
    """127.0.0.1 の受け口。受け付けた接続を数える。"""

    def __init__(self):
        self.accepted = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                sock, _ = self._server.accept()
            except OSError:
                return
            self.accepted.append(sock)

    def close(self):
        for sock in self.accepted:
            try:
                sock.close()
            except OSError:
                pass
        try:
            self._server.close()
        except OSError:
            pass


class TelnetDisposeBeforeConnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.listener = _Listener()
        self.addCleanup(self.listener.close)

    def _connect_after_a_dispose(self):
        """connect() を呼ぶ前にタブを閉じ、そのあと接続スレッドを走らせる。

        戻り値は (TelnetConnection, connect() の戻り値, 出たシグナル)。
        """
        conn = TelnetConnection("127.0.0.1", self.listener.port)
        signals = []
        conn.connected.connect(lambda: signals.append("connected"))
        conn.error_occurred.connect(lambda m: signals.append("error"))

        # 接続スレッドが動き出す前にタブが閉じられた
        conn.dispose()

        returned = conn.connect()
        self.addCleanup(conn.dispose)
        return conn, returned, signals

    def test_a_connect_after_a_dispose_is_cancelled(self):
        """破棄済みの接続では、接続を成立させずに失敗を返すこと。"""
        conn, returned, signals = self._connect_after_a_dispose()

        self.assertFalse(
            returned,
            "破棄済みなのに接続成功を返している（出たシグナル: %s）" % signals)
        self.assertFalse(conn.is_connected, "破棄済みなのに接続済みになっている")
        self.assertNotIn("connected", signals,
                         "破棄済みなのに接続成功を通知している")

    def test_a_connect_after_a_dispose_opens_no_socket(self):
        """破棄済みの接続では、機器へ TCP を張らないこと。"""
        conn, _, _ = self._connect_after_a_dispose()

        # 受け口側が accept を済ませるまでの猶予（張られていれば必ず届く）
        deadline = time.monotonic() + 1.0
        while not self.listener.accepted and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(
            len(self.listener.accepted), 0,
            "破棄済みなのに機器へ繋ぎにいっている（閉じる経路を持つ者がいない）")
        self.assertIsNone(conn.socket, "ソケットへの参照が残っている")

    def test_a_connect_after_a_dispose_starts_no_read_thread(self):
        """破棄済みの接続では、読み取りスレッドを起こさないこと。"""
        conn, _, _ = self._connect_after_a_dispose()

        thread = conn._read_thread
        self.assertTrue(
            thread is None or not thread.is_alive(),
            "破棄済みなのに読み取りスレッドが動いている: %r" % (thread,))

    def test_the_dispose_mark_survives_the_entry_of_connect(self):
        """後始末の印を connect() の入口で消さないこと。"""
        conn = TelnetConnection("127.0.0.1", self.listener.port)
        conn.dispose()
        conn.connect()
        self.addCleanup(conn.dispose)
        self.assertTrue(
            conn._stop_reading,
            "connect() の入口で後始末の印が消されている")

    def test_disconnect_still_allows_reconnecting(self):
        """利用者が明示的に切断しただけなら、同じ相手へ繋ぎ直せること。"""
        conn = TelnetConnection("127.0.0.1", self.listener.port)
        self.addCleanup(conn.dispose)

        self.assertTrue(conn.connect(), "1 回目の接続に失敗した")
        conn.disconnect()
        self.assertTrue(
            conn.connect(),
            "disconnect() のあとに繋ぎ直せない（後始末の印が残っている）")


if __name__ == "__main__":
    unittest.main()
