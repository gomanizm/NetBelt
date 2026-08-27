"""端末サイズの機器への伝達 (RFC 4254)。

pty-req (6.2) は接続時に、window-change (6.7) は接続中に使う。
実機は使わず、channel をモックにして呼び出しだけを検証する。
"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core.ssh_connection import SSHConnection   # noqa: E402


class PtySizeTest(unittest.TestCase):
    def conn(self):
        return SSHConnection("192.0.2.1", 22, "user", "pw")

    def test_the_size_set_before_connecting_feeds_the_pty_request(self):
        conn = self.conn()
        conn.set_terminal_size(120, 40)
        self.assertEqual((conn.term_cols, conn.term_rows), (120, 40))

    def test_a_live_connection_hears_window_change_at_once(self):
        conn = self.conn()
        conn.is_connected = True
        conn.channel = mock.Mock()
        conn.set_terminal_size(132, 43)
        conn.channel.resize_pty.assert_called_once_with(width=132, height=43)

    def test_a_dead_channel_does_not_take_the_app_down(self):
        conn = self.conn()
        conn.is_connected = True
        conn.channel = mock.Mock()
        conn.channel.resize_pty.side_effect = OSError("Socket is closed")
        conn.set_terminal_size(132, 43)     # 例外が漏れないこと
        self.assertEqual((conn.term_cols, conn.term_rows), (132, 43))

    def test_the_default_matches_the_advertised_vt100(self):
        conn = self.conn()
        self.assertEqual((conn.term_cols, conn.term_rows), (80, 24))


if __name__ == "__main__":
    unittest.main()
