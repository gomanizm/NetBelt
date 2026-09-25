"""Transport ができてから動き出すまでの間の dispose() でも、その Transport を閉じることを検証する。

実測で起きていたこと: paramiko 4.0.0 の SSHClient.connect は、
`t = self._transport = Transport(...)` のあと use_compression などを
設定してから t.start_client() で Transport を動かす。この間にタブが閉じ
られると、dispose() の client.close() は「まだ動いていない」Transport の
close()（active でなければ何もしない）を呼び、client._transport を None に
するだけで終わる。そのあと start_client が走って Transport が動き出し、
鍵交換まで済ませる。直後の `self._transport.gss_kex_used` が
「'NoneType' object has no attribute」で落ちて _fail() に来るが、_fail() の
client.close() は _transport が None なので何もしない。localhost の
paramiko サーバで use_compression の中で止めて dispose() すると、2 秒
経ってもクライアント側の Transport スレッドが 1 本残り、サーバ側の
セッションも生きていた。機器側のログイン猶予（既定 120 秒）まで vty の枠を
掴む。

client から Transport へたどれなくなったあとの後始末は、認証失敗などの
_fail() でも、成立してしまった接続を閉じる _abandon() でも同じく届かない。

直し方: SSHClient.connect に transport_factory を渡して、作った Transport を
connect() のローカルに覚えておく。_fail() と _abandon() は client.close() に
加えて、その Transport も直接 close() する。
"""
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

import paramiko                                     # noqa: E402

from core.ssh_connection import SSHConnection       # noqa: E402


class _RejectingServer(paramiko.ServerInterface):
    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_FAILED


class _LocalSSHServer:
    """localhost だけで待ち受け、認証は必ず拒む paramiko サーバ。"""

    def __init__(self):
        self.key = paramiko.ECDSAKey.generate()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                accepted, _ = self.sock.accept()
            except OSError:
                return
            transport = paramiko.Transport(accepted)
            transport.add_server_key(self.key)
            self.transports.append(transport)
            try:
                transport.start_server(server=_RejectingServer())
            except Exception:
                pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for transport in self.transports:
            transport.close()


def _client_transports():
    """いま動いているクライアント側の Transport スレッド"""
    return [t for t in threading.enumerate()
            if isinstance(t, paramiko.Transport) and t.is_alive()
            and not t.server_mode]


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])


class SshDisposeBeforeTransportStartsTest(_QtTestCase):
    """本物の paramiko とローカルのサーバで、実際に起きていた経路をたどる。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="netbelt-tstart-"))
        home = mock.patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.server = _LocalSSHServer()
        self.addCleanup(self.server.close)

    def test_a_transport_started_after_the_dispose_is_closed(self):
        """Transport ができた直後（動き出す前）に閉じたタブでも、Transport を残さないこと。"""
        before = {id(t) for t in _client_transports()}
        reached = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        real_use_compression = paramiko.Transport.use_compression
        gated = []

        def gated_use_compression(transport, *args, **kwargs):
            # SSHClient.connect が Transport を作った直後、start_client の前
            if not gated:
                gated.append(transport)
                reached.set()
                release.wait(10.0)
            return real_use_compression(transport, *args, **kwargs)

        conn = SSHConnection("127.0.0.1", self.server.port, "admin", password="pw")
        outcome = {}

        def attempt():
            outcome["returned"] = conn.connect()

        with mock.patch.object(paramiko.Transport, "use_compression",
                               gated_use_compression):
            worker = threading.Thread(target=attempt, daemon=True)
            worker.start()
            self.assertTrue(reached.wait(10.0), "前提: Transport が作られていない")
            conn.dispose()      # Transport はできたが、まだ動いていない
            release.set()
            worker.join(timeout=30.0)
        self.assertFalse(worker.is_alive(), "connect() が戻ってこない")
        self.assertFalse(outcome.get("returned"))

        # 閉じた Transport のスレッドが終わるのを待つ（残る場合は待っても残る）
        deadline = time.monotonic() + 2.0
        while True:
            left = [t for t in _client_transports() if id(t) not in before]
            if not left or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        for transport in left:
            # 落ちたときに、あとのテストへ Transport を持ち越さない
            self.addCleanup(transport.close)
        self.assertEqual(
            len(left), 0, "破棄したあとに動き出した Transport が閉じられずに残っている")


class SshTransportUnreachableFromClientTest(_QtTestCase):
    """client から Transport へたどれなくなったあとの、失敗と取り消しの後始末。

    paramiko の中で client._transport が消えたあとに、接続が例外で終わる
    場合（_fail）と、何事もなく戻ってくる場合（_abandon）の両方を見る。
    """

    def _dispose_then_finish(self, error):
        """Transport ができたところでタブを閉じ、そのあと error で失敗させる（None なら成功）。

        戻り値は (connect() の戻り値, 作られた Transport)。
        """
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        client = mock.Mock()
        made = []
        waiting = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)

        def fake_connect(**kwargs):
            # paramiko と同じく、渡されなければ Transport をそのまま使う
            factory = kwargs.get("transport_factory") or paramiko.Transport
            made.append(factory(mock.Mock(name="sock")))
            waiting.set()
            # ここで待っている間にタブが閉じられる。client.close() は
            # この Transport へ届かない（まだ動いていないので何もしない）
            finish.wait(5.0)
            if error is not None:
                raise error

        client.connect.side_effect = fake_connect
        outcome = {}

        def attempt():
            with mock.patch("core.ssh_connection.paramiko.SSHClient",
                            return_value=client), \
                 mock.patch("core.ssh_connection.paramiko.Transport") as transport_class, \
                 mock.patch.object(SSHConnection, "_setup_host_keys"):
                transport_class.side_effect = lambda *a, **k: mock.Mock(name="transport")
                outcome["returned"] = conn.connect()

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        self.assertTrue(waiting.wait(5.0), "前提: Transport が作られていない")
        conn.dispose()
        finish.set()
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive(), "connect() が戻ってこない")
        self.assertEqual(len(made), 1, "前提: Transport が 1 つ作られていない")
        return outcome.get("returned"), made[0]

    def test_a_failure_closes_the_transport_it_made(self):
        """認証失敗などで _fail() に来ても、作られた Transport を閉じること。"""
        for error in (paramiko.AuthenticationException("Authentication failed."),
                      paramiko.BadHostKeyException(
                          "192.0.2.1", paramiko.ECDSAKey.generate(),
                          paramiko.ECDSAKey.generate()),
                      paramiko.SSHException("Error reading SSH protocol banner"),
                      AttributeError("'NoneType' object has no attribute 'gss_kex_used'")):
            with self.subTest(error=type(error).__name__):
                returned, transport = self._dispose_then_finish(error)
                self.assertFalse(returned)
                self.assertTrue(transport.close.called,
                                "失敗したあと、作られた Transport を誰も閉じていない")

    def test_an_abandoned_connection_closes_the_transport_it_made(self):
        """破棄済みで成立した接続を _abandon() で閉じるときも、Transport を閉じること。"""
        returned, transport = self._dispose_then_finish(None)
        self.assertFalse(returned)
        self.assertTrue(transport.close.called,
                        "取り消した接続の Transport を誰も閉じていない")


if __name__ == "__main__":
    unittest.main()
