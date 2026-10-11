"""内蔵 SFTP サーバーで、開いたままにできるファイルの数に上限を掛ける件。

何が起きていたか（実測、基準 9fee4af。127.0.0.1 のみ）: 認証済みの相手は、
SFTP のセッション 1 本で読み取りのハンドルを開き続けるだけで、4.3 秒で 8189 個
開けた。開いたファイルは 1 個ずつ C ランタイム（UCRT）の低水準ファイル記述子を
使い、その上限はプロセスで 8192 個（CRT のファイル入出力の上限で、Windows の
ハンドル全体の上限ではない）なので、持っている間は同じプロセスの Python の
open() が OSError(24, 'Too many open files') で失敗した（名前解決の getaddrinfo も
同じく失敗する。QFile と確立済みのソケットは影響を受けない）。NetBelt は GUI と
各サーバーが同じプロセスで動く。チャネル数の上限（1 接続 10 本）では防げない。

利用者の決定: チャネル数の上限とは別に、開いているファイルの数に上限を持つ。

どう直したか: 1 セッションあたり MAX_OPEN_FILES_PER_SESSION 個、サーバー全体で
MAX_OPEN_FILES_TOTAL 個まで（接続をまたいだ数は錠で守る）。超えた open は
SFTP_FAILURE で断り、診断の行（種類ごとの上限つき）とパネルのログ（配送の
上限つき）へ出す。close と、セッションの終わり（finish_subsystem から呼ばれる
session_ended）で数を戻す。ディレクトリのハンドルは記述子を使わない（一覧を
メモリに持つだけ）ので数えない。
"""
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
# UCRT の低水準ファイル入出力の記述子の上限（Windows のハンドル全体の上限ではない）
UCRT_MAX_FDS = 8192


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SftpOpenFileCapTest(unittest.TestCase):
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
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-files-")
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"abc")

    def _limits(self, per_session, total):
        """上限を小さくする（マネージャを作る前に呼ぶ）"""
        from core.sftp_server import SFTPServerManager
        for name, value in (("MAX_OPEN_FILES_PER_SESSION", per_session),
                            ("MAX_OPEN_FILES_TOTAL", total)):
            p = mock.patch.object(SFTPServerManager, name, value)
            p.start()
            self.addCleanup(p.stop)

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
    def _opens(sftp):
        """sftp で a.txt をもう 1 個開けるか（開けたハンドルは返す）"""
        try:
            return sftp.open("a.txt", "rb")
        except IOError:
            return None

    def test_the_limits_leave_room_for_the_rest_of_the_process(self):
        from core.sftp_server import SFTPServerManager
        per_session = SFTPServerManager.MAX_OPEN_FILES_PER_SESSION
        total = SFTPServerManager.MAX_OPEN_FILES_TOTAL
        self.assertGreater(per_session, 0)
        self.assertLessEqual(per_session, total)
        # CRT の記述子の上限の半分以上を、設定の保存・ログ・FTP/TFTP のファイル・
        # 名前解決に残す
        self.assertLessEqual(total, UCRT_MAX_FDS // 2)

    def test_an_open_beyond_the_session_limit_is_refused(self):
        """上限 +1 個目は断られ、ほかの操作・ほかのセッションは巻き込まれない。"""
        from core.sftp_server import SFTPServerManager
        limit = SFTPServerManager.MAX_OPEN_FILES_PER_SESSION
        self._start()
        client = self._connect()
        s = client.open_sftp()
        handles = [s.open("a.txt", "rb") for _ in range(limit)]
        with self.assertRaises(IOError):
            s.open("a.txt", "rb")
        # 同じセッションの、開く以外の操作は通る
        self.assertIn("a.txt", s.listdir("."))
        self.assertEqual(s.stat("a.txt").st_size, 3)
        self.assertEqual(handles[0].read(), b"abc")
        # 同じ接続の別のセッションは開ける
        other = client.open_sftp()
        with other.open("a.txt", "rb") as f:
            self.assertEqual(f.read(), b"abc")
        # 同じプロセスの普通の open() も通る
        probe = os.path.join(self.root, "probe.txt")
        with open(probe, "w") as f:
            f.write("x")

    def test_closing_a_file_gives_its_slot_back(self):
        self._limits(per_session=3, total=100)
        self._start()
        s = self._connect().open_sftp()
        handles = [s.open("a.txt", "rb") for _ in range(3)]
        self.assertIsNone(self._opens(s), "前提: 上限で断られていない")
        handles.pop().close()
        again = self._opens(s)
        self.assertIsNotNone(again, "閉じても枠が戻らない")
        self.assertEqual(again.read(), b"abc")

    def test_a_failed_open_gives_its_slot_back(self):
        """開けなかった open（無いファイル）は枠を使わないこと。

        数えるのは開く前なので、開けなかった分はその場で戻す。戻さないと、
        無いファイルを上限の回数だけ開こうとしただけで、そのセッションは
        セッションが終わるまで何も開けなくなる
        """
        self._limits(per_session=3, total=3)
        self._start()
        s = self._connect().open_sftp()
        for _ in range(5):
            with self.assertRaises(IOError):
                s.open("missing.txt", "rb")
        handles = [self._opens(s) for _ in range(3)]
        self.assertTrue(all(h is not None for h in handles),
                        "開けなかった open が枠を使ったままになっている")
        self.assertEqual(handles[0].read(), b"abc")
        self.assertIsNone(self._opens(s), "上限を超えて開けた")

    def test_write_handles_count_toward_the_limit(self):
        """書き込みで開いたハンドルも上限に数え、閉じれば戻すこと。"""
        self._limits(per_session=3, total=100)
        self._start()
        s = self._connect().open_sftp()
        held = [s.open("w%d.bin" % i, "wb") for i in range(3)]
        self.assertEqual(len(held), 3)
        self.assertIsNone(self._opens(s), "書き込みのハンドルが上限に数えられていない")
        with self.assertRaises(IOError):
            s.open("w3.bin", "wb")
        # 断った書き込みの open は、ファイルを作らない
        self.assertFalse(os.path.exists(os.path.join(self.root, "w3.bin")))
        held.pop().close()
        self.assertIsNotNone(self._opens(s), "書き込みのハンドルを閉じても枠が戻らない")

    def test_a_close_that_raises_gives_back_only_once(self):
        """close が例外を出したハンドルを閉じ直されても、数は 1 回しか戻さないこと。

        paramiko は close が例外を出したハンドルを表に残すので、相手は同じ
        ハンドルへ CLOSE を何度でも送れる。そのたびに戻すと上限を超えて開ける
        """
        import paramiko
        from paramiko.sftp import CMD_CLOSE
        self._limits(per_session=3, total=100)
        self._start()
        s = self._connect().open_sftp()
        handles = [s.open("a.txt", "rb") for _ in range(3)]

        def broken_close(handle):
            raise RuntimeError("close failed")

        with mock.patch.object(paramiko.SFTPHandle, "close", broken_close):
            raw = handles[0].handle
            handles[0].close()  # paramiko のクライアントは失敗を握りつぶす
            for _ in range(3):
                with self.assertRaises(IOError):
                    s._request(CMD_CLOSE, raw)
        # 戻ったのは 1 個ぶんだけ（開けたハンドルは持っておく。捨てると
        # paramiko のクライアントが GC の時点で閉じ、枠が戻る）
        again = self._opens(s)
        self.assertIsNotNone(again, "close が例外を出した分が戻らない")
        self.assertIsNone(self._opens(s), "同じハンドルを閉じ直すたびに数が戻っている")

    def test_the_server_wide_limit_spans_connections(self):
        self._limits(per_session=3, total=5)
        self._start()
        a = self._connect().open_sftp()
        b = self._connect().open_sftp()
        held_a = [a.open("a.txt", "rb") for _ in range(3)]
        held_b = [b.open("a.txt", "rb") for _ in range(2)]
        self.assertEqual(len(held_b), 2)
        # b はセッションの上限（3）に達していないが、全体の上限（5）で断られる
        self.assertIsNone(self._opens(b), "サーバー全体の上限が効いていない")
        held_a.pop().close()
        self.assertIsNotNone(self._opens(b), "別の接続で閉じた分が全体へ戻らない")

    def test_closing_one_file_gives_back_only_one_to_the_server_wide_count(self):
        """何個も開いているセッションが 1 個閉じたとき、全体へ戻すのは 1 個ぶんだけ。

        セッションが持つ数をまとめて戻すと、全体の数が実際より小さくなり、
        全体の上限を超えて開ける
        """
        self._limits(per_session=3, total=4)
        self._start()
        a = self._connect().open_sftp()
        b = self._connect().open_sftp()
        held_a = [a.open("a.txt", "rb") for _ in range(3)]
        held_b = [b.open("a.txt", "rb")]
        self.assertIsNone(self._opens(b), "前提: 全体の上限（4）で断られていない")
        held_a.pop().close()
        again = self._opens(b)
        self.assertIsNotNone(again, "閉じた 1 個ぶんが全体へ戻らない")
        held_b.append(again)
        self.assertIsNone(self._opens(b), "1 個閉じただけで全体の数が 2 個以上戻っている")

    def test_ending_a_session_gives_back_what_it_left_open(self):
        """閉じないまま終わったセッションの分も、全体の数へ戻ること。"""
        self._limits(per_session=3, total=5)
        self._start()
        client = self._connect()
        a = client.open_sftp()
        b = client.open_sftp()
        left_open = [a.open("a.txt", "rb") for _ in range(3)]
        self.assertEqual(len(left_open), 3)
        held_b = [b.open("a.txt", "rb") for _ in range(2)]
        self.assertIsNone(self._opens(b), "前提: 全体の上限で断られていない")
        a.close()  # ハンドルは閉じずにセッションを終える
        self.assertTrue(self._wait(lambda: self._keep(held_b, self._opens(b))),
                        "終わったセッションの分が全体へ戻らない")
        # 全体の残り（5 - 3 = 2 個）は新しいセッションでも開ける
        c = client.open_sftp()
        held_c = [self._opens(c) for _ in range(2)]
        self.assertTrue(all(h is not None for h in held_c), "全体の数が戻りきっていない")
        self.assertIsNone(self._opens(c), "全体の上限を超えて開けた")

    @staticmethod
    def _keep(held, handle):
        """開けたハンドルを held へ足し、開けたかを返す（_wait 用）"""
        if handle is None:
            return False
        held.append(handle)
        return True

    def test_a_session_end_gives_back_even_if_closing_raises(self):
        """後始末の close が例外で止まっても、終わったセッションの分が全体へ戻ること。

        paramiko の finish_subsystem は残ったハンドルを順に close し、1 つが
        例外を出すと残りは閉じない。その前に呼ばれる session_ended が、
        セッションの残りをまとめて戻す
        """
        import paramiko
        self._limits(per_session=3, total=5)
        self._start()
        client = self._connect()
        a = client.open_sftp()
        b = client.open_sftp()
        left_open = [a.open("a.txt", "rb") for _ in range(3)]
        held_b = [b.open("a.txt", "rb") for _ in range(2)]
        self.assertEqual((len(left_open), len(held_b)), (3, 2))
        self.assertIsNone(self._opens(b), "前提: 全体の上限で断られていない")

        def broken_close(handle):
            raise RuntimeError("close failed")

        held_c = []
        with mock.patch.object(paramiko.SFTPHandle, "close", broken_close):
            a.close()  # ハンドルは閉じずにセッションを終える
            c = client.open_sftp()
            self.assertTrue(self._wait(lambda: self._keep(held_c, self._opens(c))),
                            "終わったセッションの分が全体へ戻らない")
            for _ in range(2):
                self._keep(held_c, self._opens(c))
        # 全体の残り（5 - 2 = 3 個）が開け、それより多くは開けない
        self.assertEqual(len(held_c), 3, "close の例外で全体の数が漏れた")
        self.assertIsNone(self._opens(client.open_sftp()), "全体の数を戻しすぎた")

    def test_ordinary_transfers_are_not_affected(self):
        """1 個ずつ閉じる普通の転送は、上限より多いファイルでも通ること。"""
        self._limits(per_session=3, total=3)
        self._start()
        s = self._connect().open_sftp()
        local = tempfile.mkdtemp(prefix="netbelt-sftp-files-local-")
        for i in range(10):
            src = os.path.join(local, "up%d.bin" % i)
            with open(src, "wb") as f:
                f.write(bytes([i]) * (32 * 1024))
            s.put(src, "up%d.bin" % i)
            s.get("up%d.bin" % i, os.path.join(local, "down%d.bin" % i))
            with open(os.path.join(local, "down%d.bin" % i), "rb") as f:
                self.assertEqual(f.read(), bytes([i]) * (32 * 1024))
        self.assertEqual(self.notices, [])

    def test_a_refused_open_is_reported(self):
        import core.sftp_server as sftp_server
        kinds = []
        original = sftp_server._log_limited

        def recording(kind, text):
            kinds.append(kind)
            return original(kind, text)

        lp = mock.patch.object(sftp_server, "_log_limited", recording)
        lp.start()
        self.addCleanup(lp.stop)
        self._limits(per_session=2, total=100)
        self._start()
        s = self._connect().open_sftp()
        held = [s.open("a.txt", "rb") for _ in range(2)]
        self.assertEqual(len(held), 2)
        self.assertIsNone(self._opens(s))
        self.assertIn("open limit", kinds, "断ったことが診断の行に出ない")
        self.assertTrue(self._wait(lambda: any("上限" in n for n in self.notices)),
                        "断ったことがパネルのログへ届かない: %r" % self.notices)

    def test_session_ended_does_not_raise(self):
        """残ったファイルの close が例外を出しても、session_ended は例外を外へ
        出さず、残りのファイルも閉じて数を戻すこと。

        paramiko の finish_subsystem は session_ended を呼んでからチャネルと
        残ったハンドルを閉じ、例外は握りつぶす。例外が出ると、その後の後始末が
        飛ぶ。以前のこの試験は、session_ended がもう呼ばない _OpenFiles.end の
        失敗を模していて、開いたファイルも無かったので、何も確かめずに通っていた。
        記録（print）が書けないときは test_sftp_server_log_write_failure.py で
        確かめる
        """
        import paramiko
        from core.sftp_server import SFTPServerHandler, _OpenFiles
        open_files = _OpenFiles(3, 3)
        handler = SFTPServerHandler(None, self.root, open_files=open_files)
        handles = [handler.open("/a.txt", os.O_RDONLY, paramiko.SFTPAttributes())
                   for _ in range(3)]
        for handle in handles:
            self.assertIsInstance(handle, paramiko.SFTPHandle)
        self.assertEqual(open_files._in_use, 3)
        real_close = paramiko.SFTPHandle.close

        def close_then_raise(handle):
            # 差し替えはプロセス全体に効くので、例外はこの試験のハンドルだけ
            real_close(handle)
            if any(handle is h for h in handles):
                raise RuntimeError("close failed")

        with mock.patch.object(paramiko.SFTPHandle, "close", close_then_raise):
            handler.session_ended()
        self.assertEqual([h.readfile.closed for h in handles], [True] * 3,
                         "閉じ残した")
        self.assertEqual((open_files._in_use, open_files._held), (0, {}))


if __name__ == "__main__":
    unittest.main()
