"""FTP のデータ接続に TCP keepalive を掛け、黙って消えた書き手の予約を早く外すこと。

何が起きていたか（実測、基準 441ea02。127.0.0.1 のみ）: pyftpdlib の
DTPHandler.timeout（データ接続の無通信期限）は 300 秒で、_ProgressDTP は
それをそのまま使い、TCP keepalive も掛けていなかった。期限を 2 秒へ縮めて、
A が STOR で b'AAAA' を送ったまま黙ると、B の同名の STOR は 450 が続き、
4.0 秒後にやっと 226 になった（A には 421 Data connection timed out）。
つまり黙った書き手の予約はこの期限でしか外れず、実際の値では 300 秒残る。
回線断などで相手が RST も FIN も送らずに消えると、送るものの無いサーバー側の
TCP は気づけないので、同じ扱いになる（127.0.0.1 では作れない状態なので、ここは
コードを読んでの判断）。SFTP は keepalive_seconds = 30 で消えた相手に気づいて
予約を外すが、FTP にはこの仕組みが無かった。サーバー側のデータソケットの
SO_KEEPALIVE は 0 だった。

どう直したか（利用者の決定 A）: _ProgressDTP がデータ接続を受け取った時点で
SO_KEEPALIVE を立て、Windows では SIO_KEEPALIVE_VALS で 30 秒（SFTP と同じ）
黙ったら 5 秒おきに確かめる。生きている相手は応答するだけなので、黙っている
だけの遅い機器の転送は切れない。消えた相手は確かめが尽きたところ（Windows は
10 回。おおよそ 80 秒）で切れ、未完了として予約が外れる。消えた相手の検知
そのものは 127.0.0.1 で再現できないので、ここではソケットの設定を確かめる。
"""
import socket
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402


class FtpDataConnectionKeepaliveTest(_FtpServerCase):
    def _server_data_socket(self, timeout=5.0):
        """受信中のサーバー側データ接続（DTPHandler）のソケットを返す"""
        from pyftpdlib.handlers import DTPHandler
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                # 待受スレッドが書き換えている最中に写すと RuntimeError になりうる
                handlers = list(self.m._server.ioloop.socket_map.values())
            except RuntimeError:
                handlers = []
            for handler in handlers:
                if isinstance(handler, DTPHandler) and handler.receive:
                    return handler.socket
            time.sleep(0.02)
        self.fail("受信中のデータ接続が見つからない")

    def test_upload_data_connection_has_tcp_keepalive(self):
        a = self.client()
        data = self.start_upload(a, "slow.cfg", b"AAAA")
        sock = self._server_data_socket()
        self.assertNotEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 0,
                            "データ接続に TCP keepalive が掛かっていない")
        if hasattr(socket, "TCP_KEEPIDLE"):
            # SFTP の keepalive_seconds（30 秒）と揃える。既定の 2 時間では効かない
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE), 30)
        if hasattr(socket, "TCP_KEEPINTVL"):
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL), 5)
        # 転送そのものは変わらない
        self.assertTrue(self.finish_upload(a, data, b"aaaa").startswith("226"))
        self.assertEqual(self.read("slow.cfg"), b"AAAAaaaa")

    def test_a_silent_but_alive_writer_is_not_cut(self):
        """黙っているだけの（生きている）書き手は切らず、予約も保つ"""
        a = self.client()
        data = self.start_upload(a, "quiet.cfg", b"AAAA")
        self._server_data_socket()
        time.sleep(1.0)
        b = self.client()
        with self.assertRaises(Exception) as refused:
            b.transfercmd("STOR quiet.cfg")
        self.assertIn("450", str(refused.exception))
        self.assertTrue(self.finish_upload(a, data, b"aaaa").startswith("226"))
        self.assertEqual(self.read("quiet.cfg"), b"AAAAaaaa")


if __name__ == "__main__":
    unittest.main()
