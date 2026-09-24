"""srv-01 の後始末: 書き込み中に相手が消えると、同じ保存先の予約がサーバーを
止めるまで外れない件。

何が起きていたか（実測、基準 028ebc2。127.0.0.1 のみ）: srv-01 の修正で、書き込み
中の保存先への 2 本目の書き込みは断るようになった。予約は書き込みのハンドルを
閉じたときか、その SFTP のスレッドが終わったときに外れる。ところが
SFTPServerManager は keepalive もアイドル期限も設定しておらず、接続が黙っている
間はサーバーから何も送らない。回線断などで相手が黙って消えた（TCP が半開きの）
とき、TCP は送るデータが無い限り相手の消失に気づけないので、セッションも予約も
残り続ける。その間は同じ名前へのアップロードが、サーバーを止めて起動し直すまで
すべて断られる（修正前は再送が通っていた）。

ここでは「相手が消えた」をソケットの差し替えで再現する（_VanishingPeerSocket）。
消えたあとは何も届かず、送ったデータは届かないまま、送信から少しして接続が
打ち切られる（TCP の再送が尽きたときの見え方）。送らない限り打ち切りは起きない。
修正前は、書き込みを開いた A が消えたあと 10 秒待っても、B の同じ名前への
書き込みは断られ続けた（黙っている間、サーバーが A へ送ったバイト数は 0）。

どう直したか: _handle_client で transport.set_keepalive(keepalive_seconds) を
設定する。接続が既定で 30 秒黙ると、サーバーから keepalive（応答不要の
グローバル要求）を送る。相手が消えていれば、その送信で TCP が打ち切りに至り、
セッションが終わってハンドルが閉じられ、予約が外れる。相手が生きていれば
keepalive は読み捨てられるだけなので、黙っている正規の書き手の予約は外さない。
30 秒は認証の期限（auth_timeout_seconds）と同じ桁で、FTP（pyftpdlib）の
データ接続の無通信期限 300 秒より十分短い。
"""
import errno
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
# テストでは keepalive を短くして待ち時間を減らす
KEEPALIVE = 0.3


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _VanishingPeerSocket:
    """サーバー側のソケットを包み、相手が黙って消えたときの見え方を真似る。

    vanish() のあとは相手から何も届かない（recv は待ってから timeout）。
    送ったデータは相手へ届かず、送信から ABORT_AFTER 秒後に接続が打ち切られる
    （recv が ConnectionAbortedError になる。TCP の再送が尽きたときと同じ）。
    本物の TCP と同じく、何も送らなければ打ち切りは起きない
    """

    ABORT_AFTER = 0.2

    def __init__(self, sock):
        self._sock = sock
        self._timeout = None
        self._vanished = threading.Event()
        self._aborts_at = None
        self.sent_bytes = 0
        self.sent_after_vanish = 0

    def vanish(self):
        self._vanished.set()

    def settimeout(self, timeout):
        self._timeout = timeout
        self._sock.settimeout(timeout)

    def send(self, data):
        self.sent_bytes += len(data)
        if not self._vanished.is_set():
            return self._sock.send(data)
        self.sent_after_vanish += len(data)
        if self._aborts_at is None:
            self._aborts_at = time.monotonic() + self.ABORT_AFTER
        return len(data)

    def recv(self, size):
        if not self._vanished.is_set():
            return self._sock.recv(size)
        if self._aborts_at is not None and time.monotonic() >= self._aborts_at:
            raise ConnectionAbortedError(errno.ECONNABORTED,
                                         "retransmission timed out")
        time.sleep(self._timeout or 0.1)
        raise socket.timeout("timed out")

    def __getattr__(self, name):
        return getattr(self._sock, name)


class SftpVanishedWriterReleasedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        import core.sftp_server as sftp_server
        cls._home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        cls._home.start()
        # サーバーが作る Transport にだけ、包んだソケットを渡す
        # （テスト側のクライアントは paramiko.transport から直接使うので影響しない）
        cls.peers = []
        real_transport = paramiko.Transport

        def transport(sock, *args, **kwargs):
            peer = _VanishingPeerSocket(sock)
            cls.peers.append(peer)
            return real_transport(peer, *args, **kwargs)

        cls._transport = mock.patch.object(sftp_server.paramiko, "Transport",
                                           side_effect=transport)
        cls._transport.start()
        cls.root = tempfile.mkdtemp(prefix="netbelt-sftp-vanish-")
        cls.port = free_tcp_port()
        cls.server = sftp_server.SFTPServerManager()
        cls.server.host_key = paramiko.RSAKey.generate(2048)
        cls.server.keepalive_seconds = KEEPALIVE
        assert cls.server.start(port=cls.port, root_dir=cls.root,
                                username=USER, password=PASSWORD)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._transport.stop()
        cls._home.stop()

    def connect(self):
        """接続して (SFTP クライアント, サーバー側の包んだソケット) を返す"""
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", port=self.port, username=USER,
                       password=PASSWORD, look_for_keys=False,
                       allow_agent=False, timeout=10)
        self.addCleanup(client.close)
        sftp = client.open_sftp()
        local_port = client.get_transport().sock.getsockname()[1]
        peers = [p for p in list(self.peers) if self._peer_port(p) == local_port]
        self.assertEqual(len(peers), 1, "サーバー側のソケットを特定できない")
        return sftp, peers[0]

    @staticmethod
    def _peer_port(peer):
        """包んだソケットの相手のポート。閉じ終えたソケットは None"""
        try:
            return peer.getpeername()[1]
        except OSError:
            return None

    def read(self, name):
        with open(os.path.join(self.root, name), "rb") as handle:
            return handle.read()

    def test_keepalive_is_on_by_default(self):
        """既定で keepalive が有効で、死んだ相手を 1 分以内に探りに行くこと。"""
        from core.sftp_server import SFTPServerManager
        manager = SFTPServerManager()
        self.assertGreater(manager.keepalive_seconds, 0,
                           "keepalive が無効（相手が消えても気づけない）")
        self.assertLessEqual(manager.keepalive_seconds, 60)

    def test_a_vanished_writer_frees_the_target(self):
        """書き込み中に相手が消えたら、同じ保存先へ次のアップロードが通ること。"""
        a, a_peer = self.connect()
        stuck = a.open("vanish.cfg", "w", bufsize=0)
        stuck.write(b"AAAA")
        a_peer.vanish()

        b, _ = self.connect()
        deadline = time.monotonic() + 10.0
        last_error = None
        while time.monotonic() < deadline:
            try:
                with b.open("vanish.cfg", "w") as handle:
                    handle.write(b"BBBB")
                break
            except IOError as e:
                last_error = e
                time.sleep(0.2)
        else:
            self.fail("相手が消えた書き込みの予約が 10 秒たっても外れない"
                      "（消えたあとにサーバーが送ったバイト数 %d、最後の応答 %r）"
                      % (a_peer.sent_after_vanish, last_error))
        self.assertEqual(self.read("vanish.cfg"), b"BBBB")

    def test_a_silent_but_alive_writer_keeps_the_target(self):
        """黙っているだけの生きた書き手は、keepalive を受けても予約を失わないこと。"""
        a, a_peer = self.connect()
        writer = a.open("silent.cfg", "w", bufsize=0)
        writer.write(b"AAAA")
        sent_before = a_peer.sent_bytes
        time.sleep(KEEPALIVE * 5)
        self.assertGreater(a_peer.sent_bytes, sent_before,
                           "黙っている間にサーバーが keepalive を送っていない")

        b, _ = self.connect()
        with self.assertRaises(IOError,
                               msg="生きている書き手の予約が外れている"):
            b.open("silent.cfg", "w")
        writer.write(b"aaaa")
        writer.close()
        self.assertEqual(self.read("silent.cfg"), b"AAAAaaaa")


if __name__ == "__main__":
    unittest.main()
