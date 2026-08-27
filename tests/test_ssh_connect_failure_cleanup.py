"""SSH 接続に失敗したとき、張りかけたセッションを残さないことを検証する。

connect() は例外を捕まえて error_occurred を出し return False するだけで、
self.client.close() を一度も呼んでいなかった。paramiko 4.0.0 の
SSHClient.connect() は失敗しても自分ではトランスポートを閉じないので、
機器へ張った TCP セッションと Transport スレッドが生き残る。

呼び出し側も接続失敗時は disconnect() を呼ばないため、SSHConnection を
捨てても Transport スレッド自身がオブジェクトを参照し続け、GC でも
回収されない。

多くの SSH サーバには認証前のログイン猶予があり（Cisco IOS の
ip ssh time-out、OpenSSH の LoginGraceTime、いずれも既定 120 秒）、
認証失敗で止まったセッションはその時間で機器側から切られる。そのため
実害は「パスワードを続けて打ち間違えた直後だけ vty が埋まる」一時的な
もので済む。ただし invoke_shell の失敗は認証が通ったあとなので猶予が
効かず、この場合は機器側のセッションも長く占有される。
"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402
from core.ssh_connection import SSHConnection       # noqa: E402


class SshConnectFailureCleanupTest(unittest.TestCase):
    def _attempt(self, connect_error=None, shell_error=None, password="pw"):
        """偽の SSHClient で接続を試み、(戻り値, client) を返す。"""
        conn = SSHConnection("192.0.2.1", 22, "admin", password=password)
        client = mock.Mock()
        if connect_error is not None:
            client.connect.side_effect = connect_error
        if shell_error is not None:
            client.invoke_shell.side_effect = shell_error

        with mock.patch("core.ssh_connection.paramiko.SSHClient",
                        return_value=client), \
             mock.patch.object(SSHConnection, "_setup_host_keys"), \
             mock.patch("core.ssh_connection.threading.Thread"):
            ok = conn.connect()
        return ok, client

    def test_an_authentication_failure_closes_the_client(self):
        """認証失敗のたびにセッションを残さないこと。"""
        ok, client = self._attempt(
            connect_error=paramiko.AuthenticationException("bad password"))
        self.assertFalse(ok)
        client.close.assert_called_once()

    def test_a_changed_host_key_closes_the_client(self):
        ok, client = self._attempt(
            connect_error=paramiko.BadHostKeyException(
                "192.0.2.1", mock.Mock(), mock.Mock()))
        self.assertFalse(ok)
        client.close.assert_called_once()

    def test_a_protocol_error_closes_the_client(self):
        ok, client = self._attempt(
            connect_error=paramiko.SSHException("no matching kex"))
        self.assertFalse(ok)
        client.close.assert_called_once()

    def test_any_other_error_closes_the_client(self):
        ok, client = self._attempt(
            connect_error=OSError("ホストへの経路がありません"))
        self.assertFalse(ok)
        client.close.assert_called_once()

    def test_a_shell_that_never_opens_closes_the_client(self):
        """認証は通ったがシェルが開かない場合。

        機器側のログイン猶予は認証で止まったセッションにしか効かない
        ので、ここを閉じ損ねると機器の vty が長く占有される。
        """
        ok, client = self._attempt(
            shell_error=paramiko.SSHException("channel open failed"))
        self.assertFalse(ok)
        client.close.assert_called_once()

    def test_missing_credentials_close_the_client(self):
        """資格情報が無くて始める前に諦めた場合も残さないこと。"""
        ok, client = self._attempt(password="")
        self.assertFalse(ok)
        client.close.assert_called_once()

    def test_a_successful_connection_keeps_the_client(self):
        """成功したときに閉じてしまわないこと。"""
        ok, client = self._attempt()
        self.assertTrue(ok)
        client.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
