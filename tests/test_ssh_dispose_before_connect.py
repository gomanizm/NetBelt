"""connect() が動き出す前の dispose() が、後から成立する接続を取り消すことを検証する。

タブを閉じたときの MainWindow._close_connection は、接続辞書から参照を捨てて
から dispose() を呼ぶ。接続は別スレッドで始まるので、スレッドが connect() の
本体に入る前に dispose() が走ることがある。このとき connect() は入口で後始末の
印（_stop_reading）を無条件に False へ戻してしまうため、印が消え、接続は最後まで
成立して True を返す。成立したセッションを参照しているものは誰もいないので、
閉じる経路が無く、機器の vty 枠を掴んだままプロセス終了まで残る。

client.connect() の中で待っている間の dispose() は既に塞がれている
（tests/test_ssh_dispose_during_connect.py）。ここで見るのは、その 1 行手前、
connect() がまだ何も始めていない時刻に dispose() が着地した場合。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core.ssh_connection import SSHConnection       # noqa: E402


class SshDisposeBeforeConnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _connect_after_a_dispose(self):
        """connect() を呼ぶ前にタブを閉じ、そのあと接続スレッドを走らせる。

        戻り値は (SSHConnection, connect() の戻り値, 偽 client, 出来事の並び)。
        """
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        client = mock.Mock()
        events = []

        def landed(**kwargs):
            events.append("connect-finished")

        client.connect.side_effect = landed
        client.close.side_effect = lambda: events.append("close")

        # 接続スレッドが動き出す前にタブが閉じられた
        conn.dispose()

        with mock.patch("core.ssh_connection.paramiko.SSHClient",
                        return_value=client), \
             mock.patch.object(SSHConnection, "_setup_host_keys"), \
             mock.patch.object(SSHConnection, "_read_output"):
            returned = conn.connect()

        self.addCleanup(conn.dispose)
        return conn, returned, client, events

    def test_a_connect_after_a_dispose_is_cancelled(self):
        """破棄済みの接続では、接続を成立させずに失敗を返すこと。"""
        _, returned, client, events = self._connect_after_a_dispose()

        self.assertFalse(
            returned,
            "破棄済みなのに接続成功を返している（出来事の並び: %s）" % events)
        self.assertNotIn(
            "connect-finished", events,
            "破棄済みなのに機器へ繋ぎにいっている（出来事の並び: %s）" % events)
        client.invoke_shell.assert_not_called()

    def test_a_connect_after_a_dispose_leaves_nothing_behind(self):
        """破棄済みの接続では、掴んだままの資源を残さないこと。"""
        conn, _, _, _ = self._connect_after_a_dispose()

        self.assertFalse(conn.is_connected, "破棄済みなのに接続済みになっている")
        self.assertIsNone(conn.client, "client への参照が残っている")
        self.assertIsNone(conn.channel, "チャネルへの参照が残っている")

    def test_a_connect_after_a_dispose_starts_no_read_thread(self):
        """破棄済みの接続では、読み取りスレッドを起こさないこと。"""
        conn, _, _, _ = self._connect_after_a_dispose()

        thread = conn._read_thread
        self.assertTrue(
            thread is None or not thread.is_alive(),
            "破棄済みなのに読み取りスレッドが動いている: %r" % (thread,))

    def test_the_dispose_mark_survives_the_entry_of_connect(self):
        """後始末の印を connect() の入口で消さないこと。"""
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        conn.dispose()
        with mock.patch("core.ssh_connection.paramiko.SSHClient",
                        return_value=mock.Mock()), \
             mock.patch.object(SSHConnection, "_setup_host_keys"), \
             mock.patch.object(SSHConnection, "_read_output"):
            conn.connect()
        self.assertTrue(
            conn._stop_reading,
            "connect() の入口で後始末の印が消されている")


if __name__ == "__main__":
    unittest.main()
