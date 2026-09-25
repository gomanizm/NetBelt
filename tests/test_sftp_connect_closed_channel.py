"""接続直後のホーム取得の時点で SFTP のチャンネルが閉じていたら、接続成功を返さないことを検証する。

実測（基準 097550c、src/core/sftp_manager.py:190-217）:
  機器側が SFTP サブシステムのチャンネルを VERSION の直後に閉じ、
  クライアントが normalize('.') を送る前にその通知を受け取っていると、
  paramiko は OSError('Socket is closed') を上げる。connect() はこれを
  _UNUSABLE_CONNECT_ERRORS（TimeoutError / EOFError / SSHException）の
  どれでもないとして `except Exception: self.current_path = "/"` へ流し、
    * connect() が True を返し、is_connected=True のまま connected を発火する
    * 続く list_directory は『ディレクトリ一覧取得エラー: Socket is closed』
  になっていた（localhost の実 paramiko サーバで再現。SSH のトランスポートは
  生きたまま）。切断・壊れた応答は期限切れと同じく畳むという利用者の決定
  （2026-09-20）から外れている。

直し方:
  normalize が失敗したとき、SFTP のチャンネルが閉じている
  （get_channel().closed が True そのもの）なら、期限切れ・切断の枝と同じく
  畳んで失敗を返す（close / sftp_client=None / ssh_client=None /
  error_occurred / return False）。OSError を型ごと畳む対象へ入れると、
  開いているチャンネルでの別の失敗まで接続ごと断ることになるので、
  チャンネルの状態で見分ける。開いているチャンネルでの失敗は、これまで
  どおり '/' から始める（SFTPError の扱いも変えない）。
"""
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

import paramiko
from paramiko import SFTPServer

import test_sftp_upload_tmp_mode as base   # _MemoryFS / _SftpInterface / _Auth

sys.path.insert(0, "src")


class _ClosingSFTPServer(SFTPServer):
    """VERSION を返した直後に、SFTP のチャンネルだけを閉じる機器"""

    def _send_server_version(self):
        version = super()._send_server_version()
        self.sock.close()       # SSH のトランスポートは生かしたまま
        return version


class _Server:
    """localhost の SSH サーバ。SFTP サブシステムは開いた直後に閉じる"""

    def __init__(self):
        self.fs = base._MemoryFS()
        self.key = paramiko.ECDSAKey.generate()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.key)
            t.set_subsystem_handler("sftp", _ClosingSFTPServer,
                                    base._SftpInterface, self.fs)
            t.start_server(server=base._Auth())
            self.transports.append(t)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for t in list(self.transports):
            t.close()


class SftpConnectClosedChannelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        self.errors = []
        self.connected = []
        m.error_occurred.connect(self.errors.append)
        m.connected.connect(lambda: self.connected.append(True))
        return m

    def _mock_ssh(self, failure, closed):
        """normalize が failure で終わる sftp クライアントと、それを返す SSH。

        closed に mock.DEFAULT を渡すと .closed を設定しない（Mock のまま）。
        """
        sftp = mock.Mock()
        sftp.normalize.side_effect = failure
        if closed is not mock.DEFAULT:
            sftp.get_channel.return_value.closed = closed
        ssh = mock.Mock()
        ssh.open_sftp.return_value = sftp
        return sftp, ssh

    def _assert_folded(self, m):
        self.assertFalse(m.is_connected, "閉じたチャンネルを接続中のまま残した")
        self.assertIsNone(m.sftp_client, "使えないチャンネルを掴んだまま")
        self.assertIsNone(m.ssh_client, "使えない接続を掴んだまま")
        self.assertEqual(self.connected, [], "失敗したのに接続成功を知らせた")
        self.assertEqual(self.errors, ["SFTP接続エラー: Socket is closed"])

    def test_connect_fails_when_the_channel_is_already_closed(self):
        """チャンネルが閉じていたら、型が OSError でも畳んで失敗を返すこと。"""
        m = self._manager()
        sftp, ssh = self._mock_ssh(OSError("Socket is closed"), closed=True)

        self.assertFalse(m.connect(ssh), "閉じたチャンネルで接続成功を返した")
        self._assert_folded(m)
        sftp.close.assert_called_once()

    def test_connect_fails_when_the_device_closed_the_sftp_channel(self):
        """実 paramiko: 機器が SFTP のチャンネルを閉じていたら接続成功にしないこと。"""
        server = _Server()
        self.addCleanup(server.close)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", port=server.port, username=base.USER,
                    password=base.PASSWORD, look_for_keys=False,
                    allow_agent=False, timeout=10)
        self.addCleanup(ssh.close)
        real_open = ssh.open_sftp
        seen_closed = []

        def open_then_wait_for_close():
            # normalize を送る前に、閉じた通知が届いている状況に固定する
            client = real_open()
            deadline = time.time() + 10
            while not client.get_channel().closed and time.time() < deadline:
                time.sleep(0.01)
            seen_closed.append(client.get_channel().closed)
            return client

        ssh.open_sftp = open_then_wait_for_close
        m = self._manager()

        ok = m.connect(ssh)

        self.assertEqual(seen_closed, [True], "前提: チャンネルが閉じられていない")
        self.assertFalse(ok, "閉じたチャンネルで接続成功を返した")
        self._assert_folded(m)
        self.assertTrue(ssh.get_transport().is_active(),
                        "SSH のセッションまで閉じた（端末側のもの）")

    def test_a_failure_on_an_open_channel_still_falls_back_to_root(self):
        """対照: 開いているチャンネルでの失敗は、これまでどおり '/' から始めること。"""
        m = self._manager()
        sftp, ssh = self._mock_ssh(OSError("Permission denied"), closed=False)

        self.assertTrue(m.connect(ssh), "開いているチャンネルで接続ごと断った")
        self.assertTrue(m.is_connected)
        self.assertEqual(m.get_current_path(), "/")
        self.assertEqual(self.errors, [])
        sftp.close.assert_not_called()

    def test_a_channel_state_that_is_not_true_is_not_taken_as_closed(self):
        """対照: .closed が True そのものでなければ閉じたと見なさないこと。

        Mock の属性（真と評価される Mock）を閉じたと取り違えると、
        既存のテストや差し替えで接続ごと断ることになる。
        """
        m = self._manager()
        sftp, ssh = self._mock_ssh(OSError("Permission denied"),
                                   closed=mock.DEFAULT)

        self.assertTrue(m.connect(ssh))
        self.assertEqual(m.get_current_path(), "/")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
