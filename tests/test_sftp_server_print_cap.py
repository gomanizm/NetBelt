"""内蔵 SFTP サーバーの、要求ごとの診断行（print）に上限を掛ける件。

何が起きていたか（実測、基準 441ea02。127.0.0.1 のみ。sys.stdout を数える
だけの口へ差し替えた）: 認証済みの A が busy.cfg を書き込みで開いたまま、
B が open 'wb' と存在しない名前の stat を 3 秒繰り返すと、断った open 2808 件・
stat の失敗 2808 件で、出力は 1,448,928 バイト（約 1.7GB/時）。行は
「[SFTP Server] open refused, already open for writing」と「[SFTP Server]
stat error」がそれぞれ 2808 行。SFTPServerHandler は要求ごとに無条件で
print しており、exe では sys.stdout が日付ごとのログファイル（大きさの上限も
回転も無い）なので、要求を繰り返すだけでディスクを埋められた。GUI への
通知は max_pending_notices で抑えていたが、print には上限が無かった。

利用者の決定: ハンドラの要求ごとの print を、種類ごとに 1 分 100 行＋
省略件数の 1 行に間引く。

どう直したか: sftp_server に _LogLimiter（種類ごとに WINDOW_SECONDS あたり
LIMIT 行まで出し、超えた分は数えるだけ）を置き、ハンドラの要求ごとの print を
すべて _log_limited 経由にした。省いた件数は、次の窓の最初の行の前と、
サーバーの停止のときに 1 行で出す。種類ごとに数えるので、ある種類が
溢れても別の種類の行は出る。
"""
import contextlib
import io
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
FLOOD = 500


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SftpServerPrintCapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import core.sftp_server as sftp_server
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=data_dir)
        home.start()
        self.addCleanup(home.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-print-cap-")
        # 前のテストが同じ 1 分の枠を使い切っていても影響しないよう、
        # 間引きの状態をこのテストだけの新しいものにする
        limiter_class = getattr(sftp_server, "_LogLimiter", None)
        if limiter_class is not None:
            patcher = mock.patch.object(sftp_server, "_diag_log",
                                        limiter_class())
            patcher.start()
            self.addCleanup(patcher.stop)

    def _handler(self):
        from core.sftp_server import SFTPServerHandler
        return SFTPServerHandler(None, self.root)

    @staticmethod
    def _count(text, needle):
        return sum(1 for line in text.splitlines() if needle in line)

    # --- 本題 ------------------------------------------------------------

    def test_repeated_failed_stats_are_capped_per_minute(self):
        handler = self._handler()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(FLOOD):
                handler.stat("/no-such-file.cfg")

        printed = self._count(out.getvalue(), "stat error")
        self.assertLessEqual(
            printed, LIMIT_PER_MINUTE,
            "失敗した stat の行が要求ごとに出ている（%d 要求で %d 行）"
            % (FLOOD, printed))
        self.assertGreater(printed, 0, "最初の行まで消えている")

    def test_refused_opens_over_the_network_are_capped(self):
        """実物: 書き込み中の保存先への open を B が繰り返す（repro と同じ形）"""
        import paramiko
        from core.sftp_server import SFTPServerManager

        manager = SFTPServerManager()
        self.addCleanup(manager.stop)
        manager.host_key = paramiko.RSAKey.generate(2048)
        port = free_port()
        self.assertTrue(manager.start(port=port, root_dir=self.root,
                                      username=USER, password=PASSWORD))

        def sftp():
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect("127.0.0.1", port=port, username=USER,
                           password=PASSWORD, look_for_keys=False,
                           allow_agent=False, timeout=10)
            self.addCleanup(client.close)
            return client.open_sftp()

        held = sftp().open("busy.cfg", "wb")
        held.write(b"AAAA")
        held.flush()
        self.addCleanup(held.close)
        other = sftp()
        attempts = LIMIT_PER_MINUTE + 150

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(attempts):
                with self.assertRaises(IOError):
                    other.open("busy.cfg", "wb")

        printed = self._count(out.getvalue(), "open refused")
        self.assertLessEqual(
            printed, LIMIT_PER_MINUTE,
            "断った open の行が要求ごとに出ている（%d 要求で %d 行）"
            % (attempts, printed))
        self.assertGreater(printed, 0, "最初の行まで消えている")

    def test_one_kind_flooding_does_not_hide_another(self):
        handler = self._handler()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(FLOOD):
                handler.stat("/no-such-file.cfg")
            handler.remove("/no-such-file.cfg")

        self.assertEqual(self._count(out.getvalue(), "remove error"), 1,
                         "別の種類の行まで省かれている")

    def test_the_omitted_count_is_reported_when_the_next_minute_starts(self):
        import core.sftp_server as sftp_server
        now = [1000.0]
        sftp_server._diag_log._now = lambda: now[0]
        handler = self._handler()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(FLOOD):
                handler.stat("/no-such-file.cfg")
        self.assertNotIn("suppressed", out.getvalue(),
                         "窓の途中で要約を出している")

        now[0] += 61.0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handler.stat("/no-such-file.cfg")
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("%d" % (FLOOD - LIMIT_PER_MINUTE), lines[0],
                      "省いた件数が合わない: %r" % lines)
        self.assertIn("stat error", lines[1],
                      "次の窓の最初の行が出ていない: %r" % lines)

    def test_the_omitted_count_is_reported_at_stop(self):
        """止めたあとに同じ種類の行が来なくても、省いた件数を失わない"""
        import paramiko
        from core.sftp_server import SFTPServerManager
        handler = self._handler()
        manager = SFTPServerManager()
        self.addCleanup(manager.stop)
        manager.host_key = paramiko.RSAKey.generate(2048)
        self.assertTrue(manager.start(port=free_port(), root_dir=self.root,
                                      username=USER, password=PASSWORD))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(FLOOD):
                handler.stat("/no-such-file.cfg")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            manager.stop()
        summary = [line for line in out.getvalue().splitlines()
                   if "suppressed" in line]
        self.assertEqual(len(summary), 1, out.getvalue())
        self.assertIn("stat error", summary[0])
        self.assertIn("%d" % (FLOOD - LIMIT_PER_MINUTE), summary[0])


if __name__ == "__main__":
    unittest.main()
