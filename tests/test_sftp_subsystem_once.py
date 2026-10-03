"""内蔵 SFTP サーバーで、1 本のチャネルに subsystem を起動できるのは 1 回だけにする件。

何が起きていたか（実測、基準 9fee4af。127.0.0.1 のみ）: paramiko の既定の
check_channel_subsystem_request は要求のたびにハンドラのスレッドを起こすので、
1 本のチャネルへ subsystem('sftp') を繰り返し要求するだけで、要求の数だけ SFTP の
スレッドが立った（20 回で 20 本）。チャネル数の上限（1 接続 10 本）では抑えられない。
1 回に限る最初の直しは、起動を試す前に「起動済み」と記録していたので、最初の
要求が断られたり失敗したりしたチャネルでは、正しい sftp の要求まで断っていた。

利用者の決定: 1 回に限る制限は残す。重複の要求はスレッドを作る前に断る。
記録するのは起動に実際に成功したときだけ。成功したチャネルは、セッションが
終わった後も起動し直させない。同じ接続の別のチャネルは普通に起動できること。

どう直したか: SSHServerInterface.check_channel_subsystem_request で、起動に成功した
チャネル（弱参照の集合）への要求は paramiko の既定の実装（ハンドラを作って
start する）を呼ぶ前に断る。既定の実装が True を返したときだけ記録する。
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

USER = "tester"
PASSWORD = "example-pass"
# 終わりの片付けで、テスト中に立ったスレッドを待つ合計の上限（秒）
SETTLE_SECONDS = 20.0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request_subsystem_without_reply(channel, name):
    """応答を求めない subsystem の要求を送る。

    応答を求める要求（invoke_subsystem）が断られると、paramiko のクライアントは
    そのチャネルを閉じてしまい、同じチャネルで続きを試せない。サーバーは要求を
    届いた順に処理するので、この後に送った要求や SFTP の要求より先に処理される
    """
    from paramiko.common import cMSG_CHANNEL_REQUEST
    from paramiko.message import Message
    m = Message()
    m.add_byte(cMSG_CHANNEL_REQUEST)
    m.add_int(channel.remote_chanid)
    m.add_string("subsystem")
    m.add_boolean(False)
    m.add_string(name)
    channel.transport._send_user_message(m)


class SftpSubsystemOnceTest(unittest.TestCase):
    """実際の接続（127.0.0.1）で確かめる"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        import paramiko
        cls.app = QApplication.instance() or QApplication([])
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        # 最後に動く片付け（接続を閉じ、サーバーを止めた後に動く）
        self._threads_before = set(threading.enumerate())
        self.handlers = []
        self.addCleanup(self._settle)
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        # 待ち受けは 127.0.0.1 だけにする（サーバーは 0.0.0.0 へ bind する）
        original_bind = socket.socket.bind

        def loopback_bind(sock, address):
            if sock.family == socket.AF_INET and address[0] in ("", "0.0.0.0"):
                address = ("127.0.0.1", address[1])
            return original_bind(sock, address)

        bp = mock.patch.object(socket.socket, "bind", loopback_bind)
        bp.start()
        self.addCleanup(bp.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-subsys-")
        self._count_handlers()

    def _count_handlers(self):
        """サーバーが作る SFTP のハンドラ（1 個につきスレッド 1 本）を数える。

        self.handlers に (ハンドラ, サーバー側のチャネル, SSHServerInterface) を積む。
        ハンドラのクラスは接続ごとに _handle_client が登録するので、接続より先に差し替える
        """
        import paramiko
        import core.sftp_server as sftp_server
        handlers = self.handlers = []

        class CountingSFTPServer(paramiko.SFTPServer):
            def __init__(self, channel, name, server, *args, **kwargs):
                handlers.append((self, channel, server))
                super().__init__(channel, name, server, *args, **kwargs)

        p = mock.patch.object(sftp_server, "SFTPServer", CountingSFTPServer)
        p.start()
        self.addCleanup(p.stop)

    def _start(self):
        from core.sftp_server import SFTPServerManager
        m = SFTPServerManager()
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.host_key = self.host_key
        self.addCleanup(m.stop)
        self.port = free_port()
        self.assertTrue(m.start(port=self.port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        self.assertTrue(self._wait(lambda: m.is_running), "サーバーが起動しない")
        self.manager = m
        # 本数は定数から取る（定数の無い版でも振る舞いで比べられるよう、無ければ 10）
        self.cap = getattr(m, "MAX_CHANNELS_PER_CONNECTION", 10)
        return m

    def _connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                  allow_agent=False, look_for_keys=False, timeout=10)
        self.addCleanup(c.close)
        return c.get_transport()

    @staticmethod
    def _open(transport):
        """session チャネルを開く（読み取りが返らないときに固まらないよう期限を付ける）"""
        channel = transport.open_session()
        channel.settimeout(30)
        return channel

    def _settle(self):
        """テスト中に立ったスレッドの終わりを待ち、配送待ちの Qt の知らせを処理する。

        接続を閉じてサーバーを止めた後に動く。待ちきれなくても落とさない
        （後続のテストを巻き込まないよう、できる範囲で片付ける）
        """
        deadline = time.monotonic() + SETTLE_SECONDS
        for t in threading.enumerate():
            if t in self._threads_before or t is threading.current_thread():
                continue
            t.join(max(0.0, deadline - time.monotonic()))
        from PyQt6.QtCore import QCoreApplication
        for _ in range(3):
            QCoreApplication.sendPostedEvents()
            QCoreApplication.processEvents()
        # サーバー側のチャネル（と Transport）への参照を残さない
        self.handlers.clear()

    @staticmethod
    def _wait(predicate, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return bool(predicate())

    def test_a_second_request_is_refused_before_a_handler_is_made(self):
        """(a) 起動したチャネルへの 2 回目の要求は、ハンドラ（スレッド）を作らずに断る。"""
        import paramiko
        self._start()
        channel = self._open(self._connect())
        channel.invoke_subsystem("sftp")
        self.assertEqual(len(self.handlers), 1, "前提: 1 回目の要求で起動していない")
        # 断られると、paramiko のクライアントはチャネルを閉じて SSHException を出す
        with self.assertRaises(paramiko.SSHException,
                               msg="2 回目の subsystem の要求が断られていない"):
            channel.invoke_subsystem("sftp")
        # 応答はハンドラを作るかどうかを決めた後に返る（待たずに数えてよい）
        self.assertEqual(len(self.handlers), 1,
                         "2 回目の要求でハンドラ（スレッド）が作られた")

    def test_a_refused_request_does_not_use_up_the_channel(self):
        """(b) 断られた要求（未知の名前）の後でも、同じチャネルで sftp を起動できる。"""
        import paramiko
        self._start()
        channel = self._open(self._connect())
        request_subsystem_without_reply(channel, "no-such-subsystem")
        try:
            channel.invoke_subsystem("sftp")
        except paramiko.SSHException:
            self.fail("断られた要求の後で、同じチャネルの sftp の要求まで断られた")
        sftp = paramiko.SFTPClient(channel)
        with sftp.open("after-refused.bin", "wb") as f:
            f.write(b"x" * 4096)
        self.assertEqual(sftp.stat("after-refused.bin").st_size, 4096)
        self.assertEqual(len(self.handlers), 1, "未知の名前の要求でハンドラが作られた")

    def test_a_channel_cannot_start_again_after_its_session_ended(self):
        """(c) 成功したセッションを終えたチャネルへの新しい要求は断る。

        セッションが終わるとサーバーはチャネルを閉じ、paramiko のクライアントは
        閉じたチャネルから要求を送れない。そこで、サーバー側の同じチャネルを
        paramiko の受信のスレッドと同じ入口（check_channel_subsystem_request）へ渡す
        """
        import paramiko
        self._start()
        channel = self._open(self._connect())
        channel.invoke_subsystem("sftp")
        sftp = paramiko.SFTPClient(channel)
        with sftp.open("done.bin", "wb") as f:
            f.write(b"y" * 4096)
        self.assertEqual(len(self.handlers), 1)
        handler, server_channel, server = self.handlers[0]
        # EOF を送ると、サーバーの SFTP のループが抜けてセッションが終わる
        channel.shutdown_write()
        self.assertTrue(self._wait(lambda: not handler.is_alive(), 20),
                        "前提: セッションが終わらない")
        self.assertTrue(server_channel.closed, "前提: サーバーがチャネルを閉じていない")
        self.assertFalse(server.check_channel_subsystem_request(server_channel, "sftp"),
                         "セッションを終えたチャネルで、もう一度起動できた")
        self.assertEqual(len(self.handlers), 1,
                         "セッションを終えたチャネルでハンドラが作られた")

    def test_other_channels_on_the_same_connection_start_normally(self):
        """(d) 同じ接続の別のチャネルは、上限の本数まで普通に起動して転送できる。"""
        import paramiko
        self._start()
        transport = self._connect()
        first = self._open(transport)
        first.invoke_subsystem("sftp")
        sessions = [paramiko.SFTPClient(first)]
        # 1 本目へ 2 回目の要求（断られる）。断っても 1 本目のセッションは続く
        request_subsystem_without_reply(first, "sftp")
        self.assertEqual(sessions[0].listdir("."), [])
        for _ in range(self.cap - 1):
            channel = self._open(transport)
            try:
                channel.invoke_subsystem("sftp")
            except paramiko.SSHException:
                self.fail("同じ接続の別のチャネル（%d 本目）で起動を断った"
                          % (len(sessions) + 1))
            sessions.append(paramiko.SFTPClient(channel))
        for i, sftp in enumerate(sessions):
            payload = bytes([i]) * (8192 + i)
            with sftp.open("f%d.bin" % i, "wb") as f:
                f.write(payload)
            with sftp.open("f%d.bin" % i, "rb") as f:
                self.assertEqual(f.read(), payload)
        self.assertEqual(len(self.handlers), self.cap,
                         "チャネル 1 本につきハンドラ 1 個になっていない")
        self.assertEqual(sorted(os.listdir(self.root)),
                         sorted("f%d.bin" % i for i in range(self.cap)))


class _FakeTransport:
    """paramiko の既定の check_channel_subsystem_request が使う分だけの Transport"""

    def __init__(self):
        self.handler = None

    def _get_subsystem_handler(self, name):
        if name == "sftp" and self.handler is not None:
            return self.handler, (), {}
        return None, (), {}


class _FakeChannel:
    closed = False

    def __init__(self, transport):
        self._transport = transport

    def get_transport(self):
        return self._transport


def _handler_class(made, started, fail_in=None):
    """作られた数（made）と start された数（started）を数えるハンドラのクラス。

    fail_in が "init" なら作成で、"start" なら起動で例外を出す（スレッドは作らない）
    """

    class Handler:
        def __init__(self, channel, name, server):
            made.append(channel)
            if fail_in == "init":
                raise RuntimeError("handler init failed (test)")

        def start(self):
            if fail_in == "start":
                raise RuntimeError("can't start new thread (test)")
            started.append(self)

    return Handler


class SubsystemRecordTest(unittest.TestCase):
    """SSHServerInterface の判定を、スレッドも接続も使わずに確かめる"""

    def _server(self):
        from core.sftp_server import SSHServerInterface
        return SSHServerInterface(USER, PASSWORD)

    def test_a_failed_request_is_not_recorded(self):
        """(b) 断った・失敗した要求は記録しない（同じチャネルで送り直せば起動できる）。

        ハンドラの作成や起動の例外は、実際の接続では paramiko が接続ごと切るので、
        同じチャネルでの送り直しはここで確かめる
        """
        for kind, name, fail_in in (("unknown name", "no-such-subsystem", None),
                                    ("handler init raises", "sftp", "init"),
                                    ("handler start raises", "sftp", "start")):
            with self.subTest(kind):
                server = self._server()
                transport = _FakeTransport()
                channel = _FakeChannel(transport)
                made, started = [], []
                transport.handler = _handler_class(made, started, fail_in)
                try:
                    ok = server.check_channel_subsystem_request(channel, name)
                except RuntimeError:
                    ok = False      # 例外のまま返しても、False を返してもよい
                self.assertFalse(ok)
                transport.handler = _handler_class(made, started)
                self.assertTrue(server.check_channel_subsystem_request(channel, "sftp"),
                                "失敗した要求の後で、同じチャネルの起動を断った")
                self.assertEqual(len(started), 1)

    def test_a_started_channel_is_refused_before_a_handler_is_made(self):
        """(a)(c) 起動したチャネルは、セッションが終わって閉じた後も、ハンドラを作らずに断る。"""
        server = self._server()
        transport = _FakeTransport()
        channel = _FakeChannel(transport)
        made, started = [], []
        transport.handler = _handler_class(made, started)
        self.assertTrue(server.check_channel_subsystem_request(channel, "sftp"))
        self.assertFalse(server.check_channel_subsystem_request(channel, "sftp"))
        self.assertEqual(len(made), 1, "2 回目の要求でハンドラが作られた")
        channel.closed = True       # セッションが終わり、サーバーが閉じた
        self.assertFalse(server.check_channel_subsystem_request(channel, "sftp"))
        self.assertEqual(len(made), 1, "閉じたチャネルでハンドラが作られた")

    def test_another_channel_of_the_same_connection_can_start(self):
        """(d) 1 本で起動しても、同じ接続（同じ SSHServerInterface）の別のチャネルは起動できる。"""
        server = self._server()
        transport = _FakeTransport()
        made, started = [], []
        transport.handler = _handler_class(made, started)
        first, second = _FakeChannel(transport), _FakeChannel(transport)
        self.assertTrue(server.check_channel_subsystem_request(first, "sftp"))
        self.assertTrue(server.check_channel_subsystem_request(second, "sftp"),
                        "1 本目で起動した後、同じ接続の別のチャネルの起動を断った")
        self.assertEqual(len(started), 2)


if __name__ == "__main__":
    unittest.main()
