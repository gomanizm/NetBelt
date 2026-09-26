"""認証なしの接続→即切断だけで、接続ごとの print が際限なく出る件（検査役の指摘）。

何が起きていたか（実測、aa8b35a。127.0.0.1 のみ。sys.stdout と sys.stderr を
数えるだけの口へ差し替えた）: 37998a2 でサーバー側の paramiko のログは
1 分 100 行＋省略件数の 1 行に抑えた。それでも、認証なしで TCP 接続→
即切断を 5 秒で 2787 回行うと、出力は 844,269 バイト（約 608MB/時）あり、
接続の数に比例して増え続けた。内訳は接続ごとに無条件で出る print の 3 行
（「Client connected from」「Client handler error」「Client disconnected
from」が各 2787 行）。exe では標準出力が上限も回転も無いログファイルなので、
待受ポートへ届く任意のホストが認証なしでディスクを埋められる、という元の
問題が残っていた。上限（32 接続）で断った接続の「Rejected」も同じ。

どう直したか: 接続ごとの print（Rejected・Client connected from・No channel
from・Client handler error・Client disconnected from）も、要求ごとの print と
同じ間引き口（_log_limited。種類ごとに 1 分 100 行＋省略件数の 1 行）へ通す。

確かめ方: 間引きの上限を小さくした新しい状態へ差し替え（時計も止めて、
途中で次の 1 分へ移らないようにする）、上限を超える数の接続を受けて止める。
各種類の行が上限以内で、出た行数と省略件数の合計が接続の数と一致すること
（件数を失わないこと）を見る。
"""
import contextlib
import io
import os
import re
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
LIMIT = 10          # 差し替えた間引きの上限（種類ごと）
CONNECTIONS = 25    # 上限を超える接続の数


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SftpServerConnectionPrintCapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=data_dir)
        home.start()
        self.addCleanup(home.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-conn-print-")

    def _use_limiter(self, limit=None):
        """間引きの状態をこのテストだけの新しいものにする。

        limit を渡すと上限をその値にし、時計を止めて次の 1 分へ移らない
        ようにする（混んでいて遅くなっても、窓の切り替えで数が揺れない）
        """
        import core.sftp_server as sftp_server
        limiter = sftp_server._LogLimiter()
        if limit is not None:
            limiter.LIMIT = limit
            limiter._now = lambda: 0.0
        patcher = mock.patch.object(sftp_server, "_diag_log", limiter)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _manager(self, max_clients=None):
        from core.sftp_server import SFTPServerManager
        manager = SFTPServerManager()
        self.addCleanup(manager.stop)
        manager.host_key = self.host_key
        if max_clients is not None:
            manager.max_client_connections = max_clients
        self.port = free_port()
        self.assertTrue(manager.start(port=self.port, root_dir=self.root,
                                      username=USER, password=PASSWORD))
        return manager

    def _connect_and_drop(self, manager, times):
        """認証なしで TCP 接続→即切断を times 回。後始末が終わるまで待つ。

        切る前にサーバーのバナーの 1 バイト目を待つ。受け付けられる前に
        切って止めると、待受キューに残った分は受け付けられないまま終わり、
        数が合わなくなる
        """
        for _ in range(times):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                self.assertEqual(s.recv(1), b"S", "サーバーのバナーが来ない")
            finally:
                s.close()
            deadline = time.time() + 10
            while time.time() < deadline:
                with manager._client_lock:
                    busy = [t for t in manager.client_threads if t.is_alive()]
                    pending = len(manager._client_sockets)
                if not busy and not pending:
                    break
                time.sleep(0.01)

    def _connect_and_be_rejected(self, times):
        """接続して、サーバーが断って閉じるのを待つことを times 回"""
        for _ in range(times):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                s.recv(1)   # 断られて閉じられると b'' か接続リセットで戻る
            except OSError:
                pass
            finally:
                s.close()

    @staticmethod
    def _shown(text, needle):
        return sum(1 for line in text.splitlines() if needle in line)

    @staticmethod
    def _suppressed(text, kind):
        pattern = re.compile(r"\[SFTP Server\] %s: (\d+) more line\(s\) "
                             r"suppressed" % re.escape(kind))
        return sum(int(m.group(1)) for m in pattern.finditer(text))

    def _assert_capped(self, text, needle, kind, expected):
        shown = self._shown(text, needle)
        self.assertLessEqual(
            shown, LIMIT,
            "%d 接続で「%s」が %d 行出ている（上限 %d が効いていない）"
            % (expected, needle, shown, LIMIT))
        self.assertEqual(
            shown + self._suppressed(text, kind), expected,
            "「%s」の出た行数 %d と省略件数の合計が、接続の数 %d と合わない"
            % (needle, shown, expected))

    # --- 本題 ------------------------------------------------------------

    def test_per_connection_lines_are_capped_but_counted(self):
        self._use_limiter(LIMIT)
        manager = self._manager()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self._connect_and_drop(manager, CONNECTIONS)
            manager.stop()   # 省いた件数は停止のときに出る

        text = out.getvalue()
        self._assert_capped(text, "] Client connected from",
                            "client connected", CONNECTIONS)
        self._assert_capped(text, "] Client handler error",
                            "client handler error", CONNECTIONS)
        self._assert_capped(text, "] Client disconnected from",
                            "client disconnected", CONNECTIONS)

    def test_rejected_lines_are_capped_but_counted(self):
        self._use_limiter(LIMIT)
        manager = self._manager(max_clients=0)   # すべて上限で断る
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self._connect_and_be_rejected(CONNECTIONS)
            manager.stop()

        self._assert_capped(out.getvalue(), "] Rejected ", "rejected",
                            CONNECTIONS)

    def test_a_few_connections_are_all_logged(self):
        """対照: 上限に届かない数の接続は、これまでどおり全部の行が出る"""
        self._use_limiter()
        manager = self._manager()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._connect_and_drop(manager, 3)
            manager.stop()

        text = out.getvalue()
        self.assertEqual(self._shown(text, "] Client connected from"), 3)
        self.assertEqual(self._shown(text, "] Client disconnected from"), 3)
        for kind in ("client connected", "client handler error",
                     "client disconnected"):
            self.assertEqual(self._suppressed(text, kind), 0,
                             "上限に届いていないのに省略している: %s" % kind)


if __name__ == "__main__":
    unittest.main()
