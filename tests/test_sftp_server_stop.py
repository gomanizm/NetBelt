"""SFTP サーバの停止まわりを検証する。

3 つの穴がある。

1. stop() は `if not self.is_running: return` で始まるが、is_running を
   True にするのはワーカースレッドの先頭で、start() はスレッドを起こした
   直後に戻る。start() の直後に stop() を呼ぶと、フラグがまだ立っていない
   窓では stop() が何もせずに戻り、利用者が止めたつもりの待受が
   生き残る。無改変では 610 回試して 1 回も踏まなかったが、ワーカーの
   起動が 50ms 遅れるだけで毎回踏む（ロジックの穴であって運の問題）。
   そのとき started.emit() が stopped.emit() の後に飛び、UI は
   「起動中」に戻る。

2. stop() はクライアントスレッドは join するのに、待受スレッド自身は
   join しない。FTP と同じ構造（tests/test_ftp_server_stop_joins.py）で、
   停止直後にマネージャが破棄されると、まだ走っているスレッドからの
   emit が解放済みオブジェクトへ届く。

3. accept のたびに client_threads へ追加するだけで、終わったスレッドを
   外さない。Syslog（syslog_receiver.py）は外しているので揃える。
   500 接続で 505 要素・生存 0 を実測。1 接続あたり約 2KB で OS の
   スレッドは漏れないが、サーバを止めるまで単調に増える。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

USER = "netbelt"
PASSWORD = "pw"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def can_connect(port, timeout=0.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class SftpServerStopTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        patch = mock.patch("core.firewall.ensure_inbound_allow",
                           return_value=(True, "test stub"))
        patch.start()
        self.addCleanup(patch.stop)
        # ホストキーの保存先をユーザーのホームから隔離する
        from pathlib import Path
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-stop-")

    def _manager(self):
        from core.sftp_server import SFTPServerManager
        m = SFTPServerManager()
        # テストが失敗しても待受を残さない
        self.addCleanup(lambda: m.stop() if m.server_thread else None)
        return m

    def _wrap_run_server(self, before=0.0, after=0.0):
        """ワーカーの起動前・終了後に遅れを入れる（窓を広げるため）。"""
        from core.sftp_server import SFTPServerManager
        original = SFTPServerManager._run_server

        def slow(self_, *args, **kwargs):
            time.sleep(before)
            original(self_, *args, **kwargs)
            time.sleep(after)

        patcher = mock.patch.object(SFTPServerManager, "_run_server", slow)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- 1. start() 直後の stop() ---

    def test_stop_right_after_start_really_stops(self):
        """ワーカーがフラグを立てる前に stop() しても、待受が止まること。"""
        self._wrap_run_server(before=0.05)
        m = self._manager()
        events = []
        m.started.connect(lambda: events.append("started"))
        m.stopped.connect(lambda: events.append("stopped"))
        port = free_port()
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))

        m.stop()

        # ワーカーが遅れて走り出す時間を十分に与えてから見る
        time.sleep(0.3)
        self.app.processEvents()
        self.assertFalse(can_connect(port),
                         "stop() のあとも待受が生きている")
        self.assertFalse(m.is_running)
        self.assertEqual(events[-1], "stopped",
                         "stopped の後に started が飛んでいる: %s" % events)

    # --- 2. 待受スレッドを待つ ---

    def test_stop_returns_only_after_the_serving_thread_has_exited(self):
        """stop() から戻った時点で、待受スレッドが生きていないこと。"""
        self._wrap_run_server(after=0.5)
        m = self._manager()
        self.assertTrue(m.start(port=free_port(), root_dir=self.root,
                                username=USER, password=PASSWORD))
        thread = m.server_thread
        deadline = time.time() + 5
        while not m.is_running and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(thread.is_alive())

        m.stop()

        self.assertFalse(thread.is_alive(),
                         "stop() が待受スレッドの終了を待たずに戻っている")

    # --- 3. 終わったクライアントスレッドを外す ---

    def test_finished_client_threads_are_pruned(self):
        """接続が終わるたびに、その分のスレッドが一覧から外れること。"""
        m = self._manager()
        port = free_port()
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        deadline = time.time() + 5
        while not m.is_running and time.time() < deadline:
            time.sleep(0.01)

        # SSH のバナー交換をせずに切る。ハンドラは失敗してすぐ終わる
        n = 30
        for _ in range(n):
            s = socket.socket()
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
            s.close()
            time.sleep(0.02)

        # ハンドラが全部終わるのを待つ
        deadline = time.time() + 5
        while any(t.is_alive() for t in m.client_threads) and time.time() < deadline:
            time.sleep(0.05)

        self.assertLessEqual(len(m.client_threads), 5,
                             "終わったスレッドが一覧に残り続けている: %d 件"
                             % len(m.client_threads))

    # --- 4. 黙って繋ぎっぱなしの相手がいても、停止は速く・確実に ---

    def _hold_idle_connections(self, port, n):
        """接続だけして何も送らない相手を n 本作る（バナー待ちで止まる）。"""
        held = []
        for _ in range(n):
            s = socket.socket()
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
            held.append(s)
            self.addCleanup(s.close)
            time.sleep(0.02)
        return held

    def test_stop_returns_promptly_with_idle_clients_connected(self):
        """バナーを送らない相手が何本いても、stop() が長く固まらないこと。

        ハンドラは transport.accept(timeout=20) で待つ。stop() は 1 本ずつ
        1 秒 join していたので、10 本で 10 秒、その間 UI が固まっていた。
        実測: 10 本で stop() に 10.02 秒。
        """
        m = self._manager()
        port = free_port()
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        deadline = time.time() + 5
        while not m.is_running and time.time() < deadline:
            time.sleep(0.01)
        self._hold_idle_connections(port, 5)
        time.sleep(0.3)
        self.assertGreaterEqual(
            sum(1 for t in m.client_threads if t.is_alive()), 5,
            "前提: ハンドラが待ち状態で生きている")
        started = time.monotonic()

        m.stop()

        self.assertLess(time.monotonic() - started, 3.0,
                        "黙った相手のぶんだけ stop() が固まっている")

    def test_no_client_handler_outlives_stop(self):
        """stop() から戻ったあとに、ハンドラスレッドが残っていないこと。

        残っていると、あとで client_disconnected を emit したときに、
        破棄済みのマネージャへ届いて落ちる（FTP と同じ構造）。
        実測: stop() の 4.8 秒後まで 10 本が生きていた。
        """
        m = self._manager()
        port = free_port()
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        deadline = time.time() + 5
        while not m.is_running and time.time() < deadline:
            time.sleep(0.01)
        self._hold_idle_connections(port, 5)
        time.sleep(0.3)
        handlers = [t for t in m.client_threads if t.is_alive()]
        self.assertGreaterEqual(len(handlers), 5, "前提: ハンドラが生きている")

        m.stop()

        self.assertEqual([t for t in handlers if t.is_alive()], [],
                         "stop() のあともハンドラが生きている")


if __name__ == "__main__":
    unittest.main()
