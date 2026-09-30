"""認証なしの接続→即切断だけで、サーバー側の paramiko のログが際限なく出る件。

何が起きていたか（実測、基準 441ea02。127.0.0.1 のみ。sys.stdout と
sys.stderr を数えるだけの口へ差し替えた）: 認証なしで TCP 接続→即切断を
4 秒で 1851 回行うだけで、出力は 3,389,374 バイト（約 3GB/時）。1 接続ごとに
paramiko の「Exception (server): Error reading SSH protocol banner」と
Traceback（File 行 5 つなど）が出た。paramiko の Transport（サーバー側）は
バナーの前に切られると、ロガー paramiko.transport へ ERROR で 1 行ずつ出す。
アプリは logging を設定していないので logging.lastResort が sys.stderr へ
書き、exe ではそれが上限も回転も無いログファイルになる。接続上限 32 は
切断ごとに枠が戻るので累積を止めない。待受ポートへ届く任意のホストが、
認証なしでディスクを埋められた。

利用者の決定 (B): サーバー側の paramiko のログを、要求ごとの print と同じ
間引き口（種類ごとに 1 分 100 行＋省略件数の 1 行）へ流す。

どう直したか: _handle_client で、サーバー側の Transport だけロガー名を
paramiko.transport.sftp_server に変える（set_log_channel。SFTP サブシステムと
チャネルもその子を使う）。そのロガーは上へ伝えず（propagate=False）、WARNING
以上を _log_limited("paramiko", …) へ渡すハンドラだけを持つ。SSH クライアント
側（ssh_connection / sftp_manager）が使う paramiko.transport には触れない。

確かめ方: pytest の下では root に pytest の記録用ハンドラが付くので、
lastResort（stderr）には届かない。そこで「root へ届いた paramiko の記録」を
数える。exe の root にはハンドラが無いので、root へ届く記録は lastResort が
そのままログファイルへ書く。
"""
import contextlib
import io
import logging
import os
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
LIMIT_PER_MINUTE = 100
CONNECTIONS = 20


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Counter(logging.Handler):
    """root へ届いた paramiko の記録を数える"""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        if record.name.startswith("paramiko"):
            self.records.append(record)


class SftpServerUnauthLogQuietTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        import core.sftp_server as sftp_server
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=data_dir)
        home.start()
        self.addCleanup(home.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-unauth-log-")
        # 前のテストが同じ 1 分の枠を使い切っていても影響しないよう、
        # 間引きの状態をこのテストだけの新しいものにする
        limiter_class = getattr(sftp_server, "_LogLimiter", None)
        if limiter_class is not None:
            patcher = mock.patch.object(sftp_server, "_diag_log",
                                        limiter_class())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.counter = _Counter()
        logging.getLogger().addHandler(self.counter)
        self.addCleanup(logging.getLogger().removeHandler, self.counter)

    def _manager(self):
        from core.sftp_server import SFTPServerManager
        manager = SFTPServerManager()
        self.addCleanup(manager.stop)
        manager.host_key = self.host_key
        self.port = free_port()
        self.assertTrue(manager.start(port=self.port, root_dir=self.root,
                                      username=USER, password=PASSWORD))
        return manager

    def _connect_and_drop(self, manager, times):
        """認証なしで TCP 接続→即切断を times 回。後始末が終わるまで待つ"""
        for _ in range(times):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.close()
            # 32 接続の上限で断られないよう、1 本ずつ片付くのを待つ
            deadline = time.time() + 10
            while time.time() < deadline:
                with manager._client_lock:
                    busy = [t for t in manager.client_threads if t.is_alive()]
                    pending = len(manager._client_sockets)
                if not busy and not pending:
                    break
                time.sleep(0.01)
        time.sleep(0.2)

    def test_server_side_paramiko_records_do_not_reach_the_root_logger(self):
        manager = self._manager()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self._connect_and_drop(manager, CONNECTIONS)

        leaked = [r.getMessage() for r in self.counter.records
                  if r.levelno >= logging.WARNING]
        self.assertEqual(
            leaked, [],
            "サーバー側の paramiko の記録が root へ届いている（exe では "
            "lastResort が上限なしでログファイルへ書く）: %d 件、先頭 %r"
            % (len(leaked), leaked[:3]))
        self.assertNotIn("Traceback", err.getvalue(),
                         "トレースバックが stderr へ出ている")

    def test_server_side_paramiko_lines_are_capped_but_kept(self):
        manager = self._manager()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self._connect_and_drop(manager, CONNECTIONS)

        lines = [line for line in out.getvalue().splitlines()
                 if "[SFTP Server] paramiko" in line]
        self.assertGreater(len(lines), 0,
                           "サーバー側の paramiko の診断が 1 行も残っていない")
        self.assertLessEqual(
            len(lines), LIMIT_PER_MINUTE,
            "%d 接続で paramiko の行が %d 行出ている（上限が効いていない）"
            % (CONNECTIONS, len(lines)))

    def test_client_side_paramiko_logger_is_left_alone(self):
        """対照: SSH クライアント側が使うロガーの設定は変えない"""
        self._manager()
        client_logger = logging.getLogger("paramiko.transport")
        self.assertTrue(client_logger.propagate,
                        "クライアント側のロガーの伝播まで止めている")
        self.assertEqual(
            [h for h in client_logger.handlers
             if not isinstance(h, logging.NullHandler)], [],
            "クライアント側のロガーにハンドラを足している")


if __name__ == "__main__":
    unittest.main()
