"""srv-01 の近くの経路: 書き込み中の保存先を、別の接続が「書き込み以外の要求」で
切り詰められる件。

何が起きていたか（実測、基準 028ebc2。127.0.0.1 のみ）: srv-01 の修正で、同じ
保存先への 2 本目の「書き込みの open」は断るようになった。ところが予約を見るのは
WRITE を立てた open だけだったので、A が 'w' で b"AAAA" を書いている間に、

  1) B が WRITE を立てずに READ|CREATE|TRUNC で OPEN する
  2) B が SETSTAT で size=0 を送る（paramiko の truncate）

のどちらも受け付けられた。Windows の os.open() は O_RDONLY|O_CREAT|O_TRUNC でも
相手の書きかけを 0 バイトに切り詰めるので、A が続けて b"aaaa" を書いて閉じると、
残ったファイルは 1) 2) とも b"\\x00\\x00\\x00\\x00aaaa" だった。A は成功で終わる
のに中身が壊れる、という srv-01 と同じ害になる。

どう直したか: 予約の対象を「書き込み、または O_TRUNC を伴う open」へ広げた
（モードとハンドルの選び方は変えない。読み取り専用の切り詰めハンドルも、閉じる
まで予約を持つ）。SETSTAT で st_size が指定されたときは、別の生きている接続が
その保存先を予約していれば SFTP_FAILURE を返し、パネルのログへ理由を出す。
同じ接続（書いている本人）の切り詰めと、大きさを伴わない SETSTAT（日時・
パーミッション）は、これまでどおり通す。
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


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class SftpTruncateWhileWritingRefusedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls._home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        cls._home.start()
        cls.root = tempfile.mkdtemp(prefix="netbelt-sftp-trunc-")
        cls.port = free_tcp_port()
        from core.sftp_server import SFTPServerManager
        cls.server = SFTPServerManager()
        cls.server.host_key = paramiko.RSAKey.generate(2048)
        cls.activity = []
        cls.server.client_activity.connect(
            lambda ip, message: cls.activity.append((ip, message)))
        assert cls.server.start(port=cls.port, root_dir=cls.root,
                                username=USER, password=PASSWORD)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._home.stop()

    def setUp(self):
        # 前のテストのログ（キュー経由で遅れて届く）を配り終えてから空にする
        self.app.processEvents()
        type(self).activity.clear()

    def sftp(self):
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", port=self.port, username=USER,
                       password=PASSWORD, look_for_keys=False,
                       allow_agent=False, timeout=10)
        self.addCleanup(client.close)
        return client.open_sftp()

    def real(self, name):
        return os.path.join(self.root, name)

    def read(self, name):
        with open(self.real(name), "rb") as handle:
            return handle.read()

    def raw_open(self, sftp, name, pflags):
        """OPEN を指定のフラグのまま送り、受け付けられたらハンドルを閉じる。

        受け付けられたら True。断られたら IOError（paramiko が状態を変換する）
        """
        from paramiko.sftp import CMD_CLOSE, CMD_HANDLE, CMD_OPEN
        from paramiko.sftp_attr import SFTPAttributes
        kind, msg = sftp._request(CMD_OPEN, name, pflags, SFTPAttributes())
        self.assertEqual(kind, CMD_HANDLE)
        sftp._request(CMD_CLOSE, msg.get_binary())
        return True

    def start_writer(self, name):
        """A が name を 'w' で開いて b"AAAA" を書いた状態にする（閉じない）"""
        writer = self.sftp().open(name, "w", bufsize=0)
        writer.write(b"AAAA")
        return writer

    def _wait_activity(self, *needles, timeout=5.0):
        """パネルへ渡るログ（別スレッドから届く）を待って、該当する行を返す"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            for ip, message in self.activity:
                if all(needle in message for needle in needles):
                    return ip, message
            time.sleep(0.02)
        return None

    def test_read_create_trunc_open_is_refused_while_another_client_writes(self):
        """WRITE を立てない READ|CREATE|TRUNC の OPEN でも、書き込み中なら断ること。"""
        from paramiko.sftp import (SFTP_FLAG_CREATE, SFTP_FLAG_READ,
                                   SFTP_FLAG_TRUNC)
        writer = self.start_writer("rtrunc.cfg")
        other = self.sftp()
        with self.assertRaises(IOError,
                               msg="書き込み中の保存先を READ|TRUNC の OPEN で切り詰められる"):
            self.raw_open(other, "rtrunc.cfg",
                          SFTP_FLAG_READ | SFTP_FLAG_CREATE | SFTP_FLAG_TRUNC)
        writer.write(b"aaaa")
        writer.close()
        self.assertEqual(self.read("rtrunc.cfg"), b"AAAAaaaa",
                         "先に書いていた側の中身が壊れている")

    def test_setstat_size_is_refused_while_another_client_writes(self):
        """SETSTAT の size（truncate）も、別の接続が書き込み中なら断ること。"""
        writer = self.start_writer("setstat.cfg")
        other = self.sftp()
        with self.assertRaises(IOError,
                               msg="書き込み中の保存先を SETSTAT size=0 で切り詰められる"):
            other.truncate("setstat.cfg", 0)
        writer.write(b"aaaa")
        writer.close()
        self.assertEqual(self.read("setstat.cfg"), b"AAAAaaaa",
                         "先に書いていた側の中身が壊れている")

    def test_the_refused_truncate_is_logged_with_the_client(self):
        """断った SETSTAT の理由を、相手の IP と一緒にパネルのログへ出すこと。"""
        writer = self.start_writer("tlog.cfg")
        self.addCleanup(writer.close)
        with self.assertRaises(IOError):
            self.sftp().truncate("tlog.cfg", 0)
        found = self._wait_activity("書き込み中", "tlog.cfg")
        self.assertIsNotNone(found, "断った理由がログに出ていない: %r"
                             % (self.activity,))
        self.assertEqual(found[0], "127.0.0.1")

    def test_the_writer_itself_may_truncate_its_own_target(self):
        """書いている本人（同じ接続）の truncate は、これまでどおり通すこと。"""
        sftp = self.sftp()
        with sftp.open("own.cfg", "w", bufsize=0) as handle:
            handle.write(b"AAAAAAAA")
            sftp.truncate("own.cfg", 4)
        self.assertEqual(self.read("own.cfg"), b"AAAA")

    def test_truncate_works_when_nobody_writes(self):
        """誰も書いていなければ、truncate はこれまでどおり効くこと。"""
        with open(self.real("idle.cfg"), "wb") as seed:
            seed.write(b"ORIGINAL")
        self.sftp().truncate("idle.cfg", 4)
        self.assertEqual(self.read("idle.cfg"), b"ORIG")

    def test_setstat_without_size_is_not_blocked_while_another_client_writes(self):
        """大きさを伴わない SETSTAT（日時）は、書き込み中でも妨げないこと。"""
        writer = self.start_writer("times.cfg")
        self.addCleanup(writer.close)
        stamp = 1_600_000_000
        self.sftp().utime("times.cfg", (stamp, stamp))
        self.assertEqual(int(os.stat(self.real("times.cfg")).st_mtime), stamp)

    def test_read_create_open_without_trunc_is_not_blocked(self):
        """O_TRUNC を伴わない READ|CREATE の OPEN は、書き込み中でも妨げないこと。"""
        from paramiko.sftp import SFTP_FLAG_CREATE, SFTP_FLAG_READ
        writer = self.start_writer("rcreate.cfg")
        self.assertTrue(self.raw_open(self.sftp(), "rcreate.cfg",
                                      SFTP_FLAG_READ | SFTP_FLAG_CREATE))
        writer.write(b"aaaa")
        writer.close()
        self.assertEqual(self.read("rcreate.cfg"), b"AAAAaaaa")

    def test_a_closed_trunc_handle_frees_the_target(self):
        """READ|CREATE|TRUNC のハンドルを閉じたら、次の書き込みを受け付けること。"""
        from paramiko.sftp import (SFTP_FLAG_CREATE, SFTP_FLAG_READ,
                                   SFTP_FLAG_TRUNC)
        with open(self.real("freed.cfg"), "wb") as seed:
            seed.write(b"OLD")
        self.assertTrue(self.raw_open(
            self.sftp(), "freed.cfg",
            SFTP_FLAG_READ | SFTP_FLAG_CREATE | SFTP_FLAG_TRUNC))
        self.assertEqual(self.read("freed.cfg"), b"")
        with self.sftp().open("freed.cfg", "w") as handle:
            handle.write(b"NEW")
        self.assertEqual(self.read("freed.cfg"), b"NEW")


if __name__ == "__main__":
    unittest.main()
