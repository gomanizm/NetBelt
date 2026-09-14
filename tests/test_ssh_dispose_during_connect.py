"""TCP が繋がる前の dispose() が、後から成立するセッションを回収することを検証する。

connect() は self.client を握ったまま client.connect() の中で待つ。名前解決や
TCP 接続を待っている間にタブが閉じられると、dispose() は Transport 登録前の
client.close()（この時点では何もしない）を呼んだあと self.client を None に
する。そのあと TCP と認証が最後まで成立しても、成立した Transport を参照して
いるものが誰もいないので閉じられず、クライアント側のスレッドと機器側の
セッションがプロセス終了まで残る。機器の vty / セッション枠を掴んだままになる。

さらに connect() の続きは self.client が None になったところで AttributeError
になり、利用者には「接続エラー: 'NoneType' object has no attribute
'invoke_shell'」という内部例外の文面が出る。

到達不能なホストなら sock.connect が例外になって Transport は作られないので
漏れない。名前解決が遅い相手を待ちきれずにタブを閉じた場合などに起きる。
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core.ssh_connection import SSHConnection       # noqa: E402


class SshDisposeDuringConnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _connect_with_a_dispose_midway(self):
        """client.connect() で待っている間にタブを閉じ、そのあと接続を成立させる。

        戻り値は (SSHConnection, connect() の戻り値, 偽 client,
        出来事の並び, 通知された文言) の 5 つ。
        """
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        client = mock.Mock()
        events = []
        waiting = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)

        def slow_connect(**kwargs):
            events.append("connect-started")
            waiting.set()
            # ここで待っている間にタブが閉じられる
            finish.wait(5.0)
            events.append("connect-finished")

        client.connect.side_effect = slow_connect
        client.close.side_effect = lambda: events.append("close")

        seen = []
        conn.error_occurred.connect(seen.append)
        outcome = {}

        def attempt():
            with mock.patch("core.ssh_connection.paramiko.SSHClient",
                            return_value=client), \
                 mock.patch.object(SSHConnection, "_setup_host_keys"), \
                 mock.patch.object(SSHConnection, "_read_output"):
                outcome["returned"] = conn.connect()

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        self.assertTrue(waiting.wait(5.0), "connect() が接続の待ちまで進んでいない")
        conn.dispose()          # TCP が繋がる前にタブを閉じる
        finish.set()            # そのあと TCP と認証が成立する
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive(), "connect() が戻ってこない")

        # error_occurred はワーカースレッドから emit されるのでキュー接続に
        # なる。イベントループを回さないと届かない
        for _ in range(5):
            self.app.processEvents()
            time.sleep(0.02)
        return conn, outcome.get("returned"), client, events, seen

    def test_a_session_that_lands_after_the_dispose_is_closed(self):
        """破棄したあとに成立したセッションを閉じること。"""
        _, returned, _, events, _ = self._connect_with_a_dispose_midway()

        self.assertFalse(returned, "破棄したのに接続成功を返している")
        self.assertIn("connect-finished", events, "接続が成立していない")
        after = events[events.index("connect-finished") + 1:]
        self.assertIn(
            "close", after,
            "成立したセッションを誰も閉じていない（出来事の並び: %s）" % events)

    def test_a_disposed_connection_does_not_open_a_shell(self):
        """破棄済みの接続でシェルを開きにいかないこと。"""
        _, _, client, _, _ = self._connect_with_a_dispose_midway()
        client.invoke_shell.assert_not_called()

    def test_a_disposed_connection_stays_disconnected(self):
        """破棄したあとに成立しても、接続済みとして扱わないこと。"""
        conn, _, _, _, _ = self._connect_with_a_dispose_midway()
        self.assertFalse(conn.is_connected, "破棄したのに接続済みになっている")
        self.assertIsNone(conn.client, "閉じた client への参照が残っている")
        self.assertIsNone(conn.channel, "チャネルへの参照が残っている")

    def test_a_dispose_does_not_surface_an_internal_error(self):
        """利用者がタブを閉じただけなので、内部例外の文面を出さないこと。"""
        _, _, _, _, seen = self._connect_with_a_dispose_midway()
        self.assertNotIn(
            "NoneType", " ".join(seen),
            "内部例外がそのまま利用者に出ている: %s" % seen)

    def test_connecting_again_after_a_disconnect_still_works(self):
        """後始末の印が残って、次の接続が取り消し扱いにならないこと。"""
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        client = mock.Mock()
        with mock.patch("core.ssh_connection.paramiko.SSHClient",
                        return_value=client), \
             mock.patch.object(SSHConnection, "_setup_host_keys"), \
             mock.patch.object(SSHConnection, "_read_output"):
            self.assertTrue(conn.connect())
            conn.disconnect()
            self.assertTrue(conn.connect(),
                            "切断後の再接続が取り消し扱いになっている")
            conn.dispose()


if __name__ == "__main__":
    unittest.main()
