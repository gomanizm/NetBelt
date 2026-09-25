"""TCP が繋がる前の dispose() のあとに接続が失敗しても、作られた Transport を閉じることを検証する。

connect() は client.connect() の中で待っている間、ローカル変数の client を
使い続ける。その間にタブが閉じられると、dispose() は Transport 登録前の
client.close()（この時点では何もしない）を呼んで self.client を None にする。
そのあと接続が成立した場合は _abandon(client, ...) が閉じるが、認証の拒否など
で例外になった場合は _fail() が self.dispose() を呼ぶだけで、ローカルの client
を誰も閉じていなかった。

実測: localhost の paramiko サーバが認証を必ず拒むようにし、名前解決の間に
dispose() した。認証失敗から 2 秒経っても、クライアント側の Transport
スレッドが 1 本残り、サーバ側のセッションも生きていた。画面の経路（名前解決中に
タブを閉じる）でも同じだった。AuthenticationException に限らず、Transport が
できたあとの BadHostKeyException・SSHException も同じ経路を通る。残るのは
機器側のログイン猶予（既定 120 秒）までで、その間 vty の枠を掴む。

直し方: _fail() にローカルの client を渡し、dispose() に加えてそれも閉じる。
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402

from core.ssh_connection import SSHConnection       # noqa: E402


def _failures():
    """Transport ができたあとに client.connect() が投げうる例外。"""
    return [
        paramiko.AuthenticationException("Authentication failed."),
        paramiko.BadHostKeyException(
            "192.0.2.1", paramiko.ECDSAKey.generate(),
            paramiko.ECDSAKey.generate()),
        paramiko.SSHException("Error reading SSH protocol banner"),
    ]


class SshDisposeThenConnectFailsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _dispose_then_fail(self, error):
        """client.connect() で待っている間にタブを閉じ、そのあと error で失敗させる。

        戻り値は (connect() の戻り値, 出来事の並び)。
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
            events.append("connect-raised")
            raise error

        client.connect.side_effect = slow_connect
        client.close.side_effect = lambda: events.append("close")
        outcome = {}

        def attempt():
            with mock.patch("core.ssh_connection.paramiko.SSHClient",
                            return_value=client), \
                 mock.patch.object(SSHConnection, "_setup_host_keys"):
                outcome["returned"] = conn.connect()

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        self.assertTrue(waiting.wait(5.0), "connect() が接続の待ちまで進んでいない")
        conn.dispose()          # TCP が繋がる前にタブを閉じる
        finish.set()            # そのあと Transport ができ、接続が失敗する
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive(), "connect() が戻ってこない")
        # error_occurred はキュー接続で届くので、ここで流しておく
        for _ in range(5):
            self.app.processEvents()
            time.sleep(0.02)
        return outcome.get("returned"), events

    def test_a_failure_after_the_dispose_closes_the_client(self):
        """破棄したあとに失敗した接続でも、作られた Transport を閉じること。"""
        for error in _failures():
            with self.subTest(error=type(error).__name__):
                returned, events = self._dispose_then_fail(error)

                self.assertFalse(returned)
                self.assertIn("connect-raised", events, "前提: 接続が失敗していない")
                after = events[events.index("connect-raised") + 1:]
                self.assertIn(
                    "close", after,
                    "失敗したあと、作られた Transport を誰も閉じていない"
                    "（出来事の並び: %s）" % events)


if __name__ == "__main__":
    unittest.main()
