"""内蔵 SFTP サーバーで、1 本の接続に開けるチャネル（セッション）の数に上限を掛ける件。

何が起きていたか（実測、基準 9fee4af。127.0.0.1 のみ）: 同時接続の上限
max_client_connections（32）が数えるのは TCP の接続だけで、認証を通った
1 本の接続の中では SFTP のセッションを何本でも開けた（40 本・200 本開けて、
その本数だけサーバー側にスレッドが立った）。また transport.accept() を 1 回しか
呼ばないので、2 本目以降のチャネルは閉じた後も transport.server_accepts に
参照が残り、接続が切れるまで溜まった（開閉 1000 回で 1000 個）。
1 本のチャネルへ subsystem('sftp') を繰り返し要求するだけでも、要求の数だけ
SFTP のスレッドが立った（20 回で 20 本。RFC 4254 6.5 は 1 チャネル 1 回まで）。

利用者の決定: 1 本の接続で同時に開けるチャネルは 10 本まで（OpenSSH の
MaxSessions の既定値）。閉じたら枠を返す。

どう直したか: チャネルを開く時点（check_channel_request）で数え、上限なら
OPEN_FAILED_RESOURCE_SHORTAGE で断る（相手には Resource shortage と理由が
見える。subsystem を要求しない素の session チャネルも数える）。受け入れた
チャネルは _handle_client の待ちループが accept() で取り出して閉じるまで持ち
（paramiko のチャネル表は弱参照なので、捨てると GC で閉じられる）、閉じた分は
次にチャネルを開く要求の時点で枠へ戻す。subsystem の起動は 1 チャネル 1 回に
限る。断ったことは診断の行（種類ごとの上限つき）とパネルのログ（配送の上限
つき）へ出す。
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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SftpChannelCapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        import paramiko
        cls.app = QApplication.instance() or QApplication([])
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-chan-")

    def _start(self):
        from PyQt6.QtCore import Qt
        from core.sftp_server import SFTPServerManager
        m = SFTPServerManager()
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.host_key = self.host_key
        self.addCleanup(m.stop)
        self.notices = []
        m.client_activity.connect(
            lambda ip, msg: self.notices.append(msg),
            Qt.ConnectionType.DirectConnection)
        self.port = free_port()
        self.assertTrue(m.start(port=self.port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        self.assertTrue(self._wait(lambda: m.is_running), "サーバーが起動しない")
        self.manager = m
        # 本数は定数から取る（値を変えても、落ちるのは値そのものを確かめるテストだけ）。
        # 定数の無い 9fee4af でも振る舞いの差で落ちるよう、無ければ決定の値 10 で数える
        self.cap = getattr(m, "MAX_CHANNELS_PER_CONNECTION", 10)
        return m

    def _connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                  allow_agent=False, look_for_keys=False, timeout=10)
        self.addCleanup(c.close)
        return c

    @staticmethod
    def _wait(predicate, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return bool(predicate())

    @staticmethod
    def _server_transport(client):
        """client の接続を受けているサーバー側の Transport"""
        import paramiko
        mine = client.get_transport().sock.getsockname()
        for t in threading.enumerate():
            if isinstance(t, paramiko.Transport) and t.server_mode:
                try:
                    if t.sock.getpeername() == mine:
                        return t
                except OSError:
                    pass
        return None

    @staticmethod
    def _sftp_threads(transport):
        """transport（サーバー側）の上で動いている SFTP のスレッド"""
        import paramiko
        found = []
        for t in threading.enumerate():
            channel = getattr(t, "sock", None)
            if isinstance(t, paramiko.SFTPServer) and t.is_alive() and \
                    channel is not None and channel.get_transport() is transport:
                found.append(t)
        return found

    def test_the_cap_is_ten_channels_per_connection(self):
        from core.sftp_server import SFTPServerManager
        self.assertEqual(SFTPServerManager.MAX_CHANNELS_PER_CONNECTION, 10)

    def test_the_eleventh_session_is_refused_with_resource_shortage(self):
        import paramiko
        self._start()
        client = self._connect()
        sessions = []
        for _ in range(self.cap):
            s = client.open_sftp()
            s.listdir(".")
            sessions.append(s)
        with self.assertRaises(paramiko.ChannelException) as caught:
            client.open_sftp()
        self.assertEqual(caught.exception.code,
                         paramiko.OPEN_FAILED_RESOURCE_SHORTAGE,
                         "上限を超えたチャネルを Resource shortage で断っていない")
        # 上限内のチャネルは巻き込まれていない
        for s in sessions:
            self.assertEqual(s.listdir("."), [])

    def test_bare_session_channels_count_too(self):
        """subsystem を要求しない素の session チャネルも上限に数えること。"""
        import paramiko
        self._start()
        transport = self._connect().get_transport()
        bare = [transport.open_session() for _ in range(self.cap)]
        self.assertTrue(all(not ch.closed for ch in bare))
        with self.assertRaises(paramiko.ChannelException):
            transport.open_session()

    def test_channels_not_yet_accepted_count_too(self):
        """ハンドラがまだ accept() していないチャネルも上限に数えること。

        チャネルを開く要求の処理（check_channel_request）とハンドラの accept() は
        別のスレッドで進む。受け入れてまだ取り出されていない分を数えないと、
        ハンドラが遅れている間は 11 本目以降も通る
        """
        import paramiko
        gate = threading.Event()
        original_accept = paramiko.Transport.accept

        def held_accept(transport, timeout=None):
            # サーバー側の accept() を、gate が開くまで止めておく
            if transport.server_mode:
                gate.wait(10)
            return original_accept(transport, timeout)

        ap = mock.patch.object(paramiko.Transport, "accept", held_accept)
        ap.start()
        self.addCleanup(ap.stop)
        self._start()
        client = self._connect()
        self.addCleanup(gate.set)
        transport = client.get_transport()
        held = [transport.open_session() for _ in range(self.cap)]
        with self.assertRaises(paramiko.ChannelException):
            transport.open_session()
        server = self._server_transport(client).server_object
        self.assertEqual(server._channels, [], "前提: ハンドラが先に accept() している")
        # ハンドラが取り出した後も、数は上限の本数のまま
        gate.set()
        self.assertTrue(self._wait(lambda: len(server._channels) == self.cap),
                        "受け入れたチャネルがハンドラに渡らない")
        with self.assertRaises(paramiko.ChannelException):
            transport.open_session()
        self.assertTrue(all(not ch.closed for ch in held))

    def test_the_first_channel_is_not_missed_when_its_subsystem_comes_first(self):
        """subsystem の要求が最初の accept() より先に届いても、1 本目を取り逃がさないこと。

        server_accepts から外すのは accept() だけにする。subsystem の要求の中で
        外すと、最初の accept(timeout=20) が 1 本目を取り逃がし、20 秒後に
        動いているセッションごと接続を切る（そう作った試作で実測: 60 回中 3 回）
        """
        import paramiko
        from core.sftp_server import SSHServerInterface
        subsystem_done = threading.Event()
        accepted = []
        original_subsystem = SSHServerInterface.check_channel_subsystem_request
        original_accept = paramiko.Transport.accept

        def subsystem(server, channel, name):
            try:
                return original_subsystem(server, channel, name)
            finally:
                subsystem_done.set()

        def late_accept(transport, timeout=None):
            # 最初の accept() を、1 本目の subsystem の要求を処理し終えるまで遅らせる
            if transport.server_mode and not subsystem_done.is_set():
                subsystem_done.wait(10)
            channel = original_accept(transport, timeout)
            if transport.server_mode and channel is not None:
                accepted.append(channel)
            return channel

        for target, name, replacement in (
                (SSHServerInterface, "check_channel_subsystem_request", subsystem),
                (paramiko.Transport, "accept", late_accept)):
            p = mock.patch.object(target, name, replacement)
            p.start()
            self.addCleanup(p.stop)
        self._start()
        client = self._connect()
        self.addCleanup(subsystem_done.set)
        s = client.open_sftp()
        self.assertTrue(subsystem_done.wait(5), "前提: subsystem の要求が届いていない")
        self.assertTrue(self._wait(lambda: len(accepted) == 1, 3),
                        "最初の accept() が 1 本目を取り逃がした")
        self.assertEqual(s.listdir("."), [])

    def test_a_closed_channel_gives_its_slot_back(self):
        self._start()
        client = self._connect()
        sessions = [client.open_sftp() for _ in range(self.cap)]
        sessions.pop().close()
        # 閉じた直後に開き直せる（閉じる知らせは開く要求より先に届く）
        again = client.open_sftp()
        self.assertEqual(again.listdir("."), [])
        again.close()
        for s in sessions:
            s.close()
        # 全部閉じれば、もう一度上限の本数まで開ける
        reopened = [client.open_sftp() for _ in range(self.cap)]
        self.assertEqual(len(reopened), self.cap)

    def test_ten_sessions_can_transfer_at_the_same_time(self):
        self._start()
        client = self._connect()
        sessions = [client.open_sftp() for _ in range(self.cap)]
        errors = []
        local = tempfile.mkdtemp(prefix="netbelt-sftp-chan-local-")

        def transfer(i, s):
            try:
                payload = bytes([i]) * (64 * 1024 + i)
                with s.open("f%d.bin" % i, "wb") as f:
                    f.write(payload)
                out = os.path.join(local, "g%d.bin" % i)
                s.get("f%d.bin" % i, out)
                with open(out, "rb") as f:
                    if f.read() != payload:
                        errors.append("content mismatch %d" % i)
            except Exception as e:
                errors.append(repr(e))

        threads = [threading.Thread(target=transfer, args=(i, s))
                   for i, s in enumerate(sessions)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(os.listdir(self.root)), self.cap)

    def test_reopening_on_one_connection_does_not_accumulate(self):
        """同じ接続で開閉を 100 回繰り返しても、参照もスレッドも溜まらないこと。"""
        self._start()
        client = self._connect()
        first = client.open_sftp()
        first.listdir(".")
        transport = self._server_transport(client)
        self.assertIsNotNone(transport, "前提: サーバー側の Transport が見つからない")
        threads_before = threading.active_count()
        for _ in range(100):
            s = client.open_sftp()
            s.listdir(".")
            s.close()
        self.assertTrue(self._wait(lambda: len(self._sftp_threads(transport)) <= 1),
                        "閉じたセッションのスレッドが残っている")
        self.assertLessEqual(len(transport.server_accepts), 1,
                             "閉じたチャネルの参照が server_accepts に溜まっている")
        self.assertLessEqual(threading.active_count(), threads_before + 2,
                             "開閉のたびにスレッドが増えている")
        self.assertEqual(first.listdir("."), [])

    def test_only_one_subsystem_per_channel(self):
        """1 本のチャネルへ subsystem を繰り返し要求しても、スレッドは 1 本だけ。"""
        import paramiko
        self._start()
        client = self._connect()
        channel = client.get_transport().open_session()
        channel.invoke_subsystem("sftp")
        server_side = self._server_transport(client)
        self.assertIsNotNone(server_side, "前提: サーバー側の Transport が見つからない")
        self.assertTrue(self._wait(lambda: len(self._sftp_threads(server_side)) == 1))
        refused = False
        for _ in range(5):
            try:
                channel.invoke_subsystem("sftp")
            except paramiko.SSHException:
                # 断られたチャネルは paramiko のクライアントが閉じる
                refused = True
                break
        time.sleep(0.3)
        self.assertLessEqual(len(self._sftp_threads(server_side)), 1,
                             "同じチャネルの subsystem の要求ごとにスレッドが立つ")
        self.assertTrue(refused, "2 回目の subsystem の要求が断られていない")

    def test_a_refused_channel_is_reported(self):
        import paramiko
        import core.sftp_server as sftp_server
        kinds = []
        original = sftp_server._log_limited

        def recording(kind, text):
            kinds.append(kind)
            return original(kind, text)

        lp = mock.patch.object(sftp_server, "_log_limited", recording)
        lp.start()
        self.addCleanup(lp.stop)
        self._start()
        transport = self._connect().get_transport()
        # 参照を持っておく（捨てるとクライアント側の GC で閉じられる）
        held = [transport.open_session() for _ in range(self.cap)]
        self.assertEqual(len(held), self.cap)
        with self.assertRaises(paramiko.ChannelException):
            transport.open_session()
        self.assertIn("channel refused", kinds, "断ったことが診断の行に出ない")
        self.assertTrue(self._wait(lambda: any("上限" in n for n in self.notices)),
                        "断ったことがパネルのログへ届かない: %r" % self.notices)


if __name__ == "__main__":
    unittest.main()
