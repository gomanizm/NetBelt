"""SETSTAT の size（切り詰め）で、確認と切り詰めの間に別の接続が書き始められる件。

何が起きていたか（実測、基準 87514a3。127.0.0.1 のみ）: 書き込み中の保存先への
SETSTAT size は断るようになっていたが、確認（_OpenWriters.held_by_other）は
その場で錠を取って見るだけで、予約は取らずに戻っていた。確認のあとの
os.truncate() までの間に、別の接続の OPEN が予約を取って書き始められる。
B の確認の直後で止め、その間に A が同じファイルを 'w' で開いて b"AAAA" を書き、
B の size=0 を進め、A が続けて b"aaaa" を書いて閉じると、A も B も成功で
終わるのに、残ったファイルは b"\\x00\\x00\\x00\\x00aaaa" だった（A の先頭が
消える）。差し込みなしでも、保存先が遅い（切り詰めに 20ms かかる共有フォルダ
相当）と、15 秒で A の書き込み 7843 回のうち 79 回が同じ形に壊れた
（ローカルディスクでは 6058 回の切り詰めで 0 回）。

どう直したか: 切り詰めの前に、確認と予約を同じ錠の中で済ませる
（_OpenWriters.reserve_for_truncate）。別の接続が予約していれば、これまで
どおり断る。誰も予約していなければ切り詰めの間だけ予約を取り、終わったら
（失敗しても）外す。書いている本人の切り詰めは、ハンドルの予約をそのまま
使って通す（外さない）。切り詰めの途中に来た別の接続の書き込みの open は、
ほかの書き込み中と同じく断られる。
"""
import os
import socket
import sys
import tempfile
import threading
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


class SftpTruncateCheckRaceTest(unittest.TestCase):
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
        cls.root = tempfile.mkdtemp(prefix="netbelt-sftp-truncrace-")
        cls.port = free_tcp_port()
        from core.sftp_server import SFTPServerManager
        cls.server = SFTPServerManager()
        cls.server.host_key = paramiko.RSAKey.generate(2048)
        assert cls.server.start(port=cls.port, root_dir=cls.root,
                                username=USER, password=PASSWORD)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._home.stop()

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

    def seed(self, name, data):
        with open(self.real(name), "wb") as handle:
            handle.write(data)

    def read(self, name):
        with open(self.real(name), "rb") as handle:
            return handle.read()

    def pause_truncate(self, name):
        """name への os.truncate を、実際に切り詰める直前で 1 回だけ止める。

        書き込み中かの確認は済み、切り詰めはまだ、という間を作る。
        (止まったことの合図, 進める合図) を返す
        """
        real_truncate = os.truncate
        reached = threading.Event()
        resume = threading.Event()
        armed = [True]

        def truncate(path, length):
            if armed[0] and os.path.basename(os.fspath(path)) == name:
                armed[0] = False
                reached.set()
                resume.wait(10)
            return real_truncate(path, length)

        patcher = mock.patch.object(os, "truncate", truncate)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 途中で落ちても、止めたサーバー側のスレッドを残さない
        self.addCleanup(resume.set)
        return reached, resume

    def truncate_in_background(self, sftp, name, size):
        """別スレッドで truncate を送る。(スレッド, 結果の入れ物) を返す"""
        result = {}

        def run():
            try:
                sftp.truncate(name, size)
                result["outcome"] = "ok"
            except IOError as e:
                result["outcome"] = e

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result

    def test_a_writer_cannot_slip_in_between_the_check_and_the_truncate(self):
        """確認の直後に別の接続が開いて書いても、その書きかけを切り詰めないこと。"""
        self.seed("race.cfg", b"OLDCONTENT")
        truncater = self.sftp()
        writer_sftp = self.sftp()
        reached, resume = self.pause_truncate("race.cfg")
        thread, result = self.truncate_in_background(truncater, "race.cfg", 0)
        self.assertTrue(reached.wait(10), "切り詰めまで届かない")

        writer = None
        try:
            writer = writer_sftp.open("race.cfg", "w", bufsize=0)
            writer.write(b"AAAA")
        except IOError:
            writer = None
        resume.set()
        thread.join(10)
        self.assertEqual(result.get("outcome"), "ok",
                         "誰も書いていないときの切り詰めが通らない")
        if writer is not None:
            writer.write(b"aaaa")
            writer.close()
            self.assertEqual(self.read("race.cfg"), b"AAAAaaaa",
                             "確認と切り詰めの間に開いた書き手の先頭が消えた")
        self.assertIsNone(writer,
                          "切り詰めの途中で、同じ保存先への書き込みの open が通った")
        self.assertEqual(self.read("race.cfg"), b"")

    def test_the_target_is_free_again_after_the_truncate(self):
        """切り詰め終えたら予約は外れ、次の書き込みを受け付けること。"""
        self.seed("after.cfg", b"ORIGINAL")
        truncater = self.sftp()
        truncater.truncate("after.cfg", 4)
        self.assertEqual(self.read("after.cfg"), b"ORIG")
        with self.sftp().open("after.cfg", "w") as handle:
            handle.write(b"NEW")
        self.assertEqual(self.read("after.cfg"), b"NEW")

    def test_a_failed_truncate_does_not_keep_the_target_reserved(self):
        """切り詰めに失敗しても（存在しない名前）、予約を残さないこと。"""
        truncater = self.sftp()
        with self.assertRaises(IOError):
            truncater.truncate("missing.cfg", 0)
        # 失敗した側の接続は生きたまま。予約が残っていれば断られる
        with self.sftp().open("missing.cfg", "w") as handle:
            handle.write(b"NEW")
        self.assertEqual(self.read("missing.cfg"), b"NEW")

    def test_the_writers_own_truncate_keeps_its_reservation(self):
        """書いている本人が切り詰めても、書き終えるまで予約を手放さないこと。"""
        sftp = self.sftp()
        other = self.sftp()
        with sftp.open("own.cfg", "w", bufsize=0) as handle:
            handle.write(b"AAAAAAAA")
            sftp.truncate("own.cfg", 4)
            with self.assertRaises(IOError,
                                   msg="本人の切り詰めのあと、別の接続が書き込みで開けた"):
                other.open("own.cfg", "w")
        self.assertEqual(self.read("own.cfg"), b"AAAA")
        # 閉じたあとは、別の接続も書ける
        with other.open("own.cfg", "w") as handle:
            handle.write(b"NEW")
        self.assertEqual(self.read("own.cfg"), b"NEW")


if __name__ == "__main__":
    unittest.main()
