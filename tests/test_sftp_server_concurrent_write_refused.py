"""srv-01: 内蔵 SFTP サーバーへ同じ保存先を並行して書くと、中身が混ざる件。

何が起きていたか（実測、基準 470c538。127.0.0.1 のみ）: 認証済みの 2 接続から
同じファイルを書き込みで開き、

  1) A が開いて b"AAAA" を書く
  2) B が開く（O_TRUNC で切り詰め）、b"BBBBBBBB" を書いて閉じる
  3) A が自分の位置（4）から b"aaaaaaaa" を書いて閉じる

とすると、A も B も成功で終わるのに、残ったファイルは b"BBBBaaaaaaaa" だった
（どちらの送信内容とも一致しない）。SFTPServerHandler.open() は要求のフラグを
そのまま os.open() へ渡すだけで、_OpenWriters は停止後の生き残りを見分ける
ための登録しかしておらず、先に書き込み中の相手がいても断らなかった。
機器の config を受け取る道具なので、複数台が同じ名前で送ってくると
黙って壊れた内容が残る。

どう直したか: 内蔵 TFTP の「同じ保存先への二重アップロードを断る」と同じ
考え方にした。書き込みで開く前に、保存先を _OpenWriters に予約する（鍵は
realpath 済みの実パスを os.path.normcase で揃えたもの。大文字小文字・区切り
文字の違いは Windows と同じく同一視する）。先客がいれば os.open() の前に
SFTP_FAILURE を返し、パネルのログへ日本語で理由を出す。予約は書き込みの
ハンドルを閉じ終えたとき（開けなかったときはその場）に外すので、閉じたあとは
次の書き込みを受け付ける。読み取りの open は予約を見ないので妨げない。
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


class SftpConcurrentWriteRefusedTest(unittest.TestCase):
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
        cls.root = tempfile.mkdtemp(prefix="netbelt-sftp-samepath-")
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

    def real(self, *parts):
        return os.path.join(self.root, *parts)

    def read(self, *parts):
        with open(self.real(*parts), "rb") as handle:
            return handle.read()

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

    def test_a_second_writer_is_refused_and_the_content_is_not_mixed(self):
        """先に書いている相手がいる間、別の接続の書き込みを断り、中身を混ぜないこと。"""
        a, b = self.sftp(), self.sftp()
        first = a.open("mixed.cfg", "w", bufsize=0)
        first.write(b"AAAA")

        with self.assertRaises(IOError,
                               msg="書き込み中の保存先を別の接続が切り詰めて書けている"):
            second = b.open("mixed.cfg", "w", bufsize=0)
            second.write(b"BBBBBBBB")
            second.close()

        first.write(b"aaaaaaaa")
        first.close()
        self.assertEqual(self.read("mixed.cfg"), b"AAAAaaaaaaaa",
                         "先に書いていた側の中身が壊れている")

    def test_the_refusal_is_logged_in_japanese_with_the_client(self):
        """断った理由を、どの相手かと一緒にパネルのログへ日本語で出すこと。"""
        a, b = self.sftp(), self.sftp()
        first = a.open("logged.cfg", "w")
        self.addCleanup(first.close)
        with self.assertRaises(IOError):
            b.open("logged.cfg", "w")

        found = self._wait_activity("書き込み中", "logged.cfg")
        self.assertIsNotNone(found, "断った理由がログに出ていない: %r"
                             % (self.activity,))
        ip, _message = found
        self.assertEqual(ip, "127.0.0.1")

    def test_case_and_separator_differences_are_the_same_target(self):
        """大文字小文字・区切り文字だけが違う名前も、同じ保存先として断ること。"""
        os.makedirs(self.real("dir"), exist_ok=True)
        a, b = self.sftp(), self.sftp()
        first = a.open("/dir/Case.cfg", "w")
        self.addCleanup(first.close)
        for other in ("dir/case.CFG", "\\DIR\\CASE.cfg", "//dir//case.cfg"):
            with self.subTest(name=other):
                with self.assertRaises(IOError,
                                       msg="%r が同じ保存先として扱われていない" % other):
                    b.open(other, "w")

    def test_the_target_is_free_again_once_the_first_writer_closes(self):
        """先の書き込みを閉じたら、次の書き込みを受け付けること。"""
        a, b = self.sftp(), self.sftp()
        with a.open("again.cfg", "w") as first:
            first.write(b"first")
        with b.open("again.cfg", "w") as second:
            second.write(b"second")
        self.assertEqual(self.read("again.cfg"), b"second")

    def test_reading_is_not_blocked_while_another_client_writes(self):
        """書き込み中の保存先でも、読み取りの open は妨げないこと。"""
        with open(self.real("read.cfg"), "wb") as seed:
            seed.write(b"ORIGINAL")
        a, b = self.sftp(), self.sftp()
        writer = a.open("read.cfg", "r+")
        self.addCleanup(writer.close)
        with b.open("read.cfg", "r") as reader:
            self.assertEqual(reader.read(), b"ORIGINAL")

    def test_writers_on_different_targets_are_not_blocked(self):
        """別の保存先への並行アップロードは、これまでどおり通ること。"""
        a, b = self.sftp(), self.sftp()
        with a.open("one.cfg", "w") as one, b.open("two.cfg", "w") as two:
            one.write(b"one")
            two.write(b"two")
        self.assertEqual(self.read("one.cfg"), b"one")
        self.assertEqual(self.read("two.cfg"), b"two")

    def test_a_failed_open_does_not_keep_the_target_reserved(self):
        """開けなかった書き込み（既存への排他作成）が、保存先を塞ぎ続けないこと。"""
        with open(self.real("exists.cfg"), "wb") as seed:
            seed.write(b"OLD")
        a = self.sftp()
        # 'x' だけだと paramiko は CREATE|EXCL しか立てず（WRITE なし）、
        # サーバーは読み取りの open として扱うので予約そのものが起きない。
        # 'wx' にして、予約を取ってから os.open() が失敗する経路を通す
        with self.assertRaises(IOError):
            a.open("exists.cfg", "wx")
        with a.open("exists.cfg", "w") as handle:
            handle.write(b"NEW")
        self.assertEqual(self.read("exists.cfg"), b"NEW")


if __name__ == "__main__":
    unittest.main()
