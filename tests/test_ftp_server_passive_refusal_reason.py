# -*- coding: utf-8 -*-
"""passive の範囲を断ったときのログが、NetBelt 自身の受信機能も理由に挙げることを検証する。

範囲の番号は、内蔵 SFTP サーバーや TCP の Syslog など NetBelt 自身の受信機能が
0.0.0.0 で排他に待ち受けても塞がる。ログの理由は「ほかの接続の PASV や、
ほかのアプリが使用中」だけで、自分の別の機能が原因のときに見当がつかなかった。
"""
import ftplib
import socket
import sys
import unittest

sys.path.insert(0, "src")
sys.path.insert(0, "tests")

from test_ftp_server_passive_range_exhausted import _ExhaustedRangeCase  # noqa: E402


@unittest.skipUnless(hasattr(socket, "SO_EXCLUSIVEADDRUSE"),
                     "SO_EXCLUSIVEADDRUSE は Windows だけ")
class PassiveRefusalReasonTest(_ExhaustedRangeCase):
    """範囲の番号を、NetBelt の受信機能と同じ形（0.0.0.0・排他）で塞ぐ"""

    def _make_blocker(self):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("0.0.0.0", 0))
        sock.listen(1)
        return sock

    def test_the_log_names_netbelt_own_receivers(self):
        ftp = self.client()
        with self.assertRaises(ftplib.error_temp):
            ftp.sendcmd("PASV")
        found = self._wait_activity("passiveポート範囲 %d-%d" % (self.y, self.y))
        self.assertIsNotNone(found, self.activity)
        message = found[1]
        for needle in ("ほかの接続の PASV", "SFTP", "Syslog", "ほかのアプリ"):
            with self.subTest(needle=needle):
                self.assertIn(needle, message)


if __name__ == "__main__":
    unittest.main()
