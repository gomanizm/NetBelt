"""known_hosts の読み書きに失敗したとき、ホスト鍵の検証が黙って弱まらないことを検証する。

_setup_host_keys は known_hosts の読み込み例外を握りつぶして TOFU
（初回の鍵を信用して保存する方針）を設定していた。既存のファイルが
読めない（権限・破損・ロック）と、既知の機器でも「未知」扱いになり、
鍵が変わっていても気づかずにパスワードを送る。保存の失敗も無視して
いたので、次回も任意の鍵を受け入れ続ける。

読めないときは接続を拒否する（鍵を検証できない状態で認証へ進まない）。
保存できないときは接続は続けるが、次回検証できないことを画面に出す。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class SshKnownHostsFailuresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="netbelt-kh-"))
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=self.data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        # 既知ホストのファイルは「存在する」状態にする
        (self.data_dir / "known_hosts").write_text(
            "192.0.2.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample\n",
            encoding="utf-8")

    def _connect(self, client):
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        errors, output = [], []
        conn.error_occurred.connect(errors.append)
        conn.output_received.connect(output.append)
        with mock.patch("core.ssh_connection.paramiko.SSHClient",
                        return_value=client), \
             mock.patch.object(SSHConnection, "_read_output"):
            ok = conn.connect()
        return conn, ok, errors, output

    def test_an_unreadable_known_hosts_refuses_the_connection(self):
        """読めないなら、検証できない状態で認証へ進まないこと。"""
        client = mock.Mock()
        client.load_host_keys.side_effect = PermissionError(13, "denied")

        conn, ok, errors, _ = self._connect(client)

        self.assertFalse(ok, "known_hosts を読めないのに接続している")
        client.connect.assert_not_called()
        self.assertTrue(any("known_hosts" in e for e in errors),
                        "理由を言っていない: %s" % errors)
        # 壊れた行が 1 つあるだけでも全接続が止まるので、直し方まで言うこと
        self.assertTrue(any(("修正" in e or "削除" in e) for e in errors),
                        "対処（該当行の修正・削除）を案内していない: %s" % errors)

    def test_a_readable_known_hosts_still_connects(self):
        """対照: 読めれば、これまでどおり接続する。"""
        client = mock.Mock()
        conn, ok, errors, _ = self._connect(client)
        self.assertTrue(ok, errors)
        client.load_host_keys.assert_called_once()

    def test_a_failed_save_of_a_new_key_is_reported_on_screen(self):
        """保存できなければ、次回検証できないことを知らせること。"""
        client = mock.Mock()
        conn, ok, errors, output = self._connect(client)
        self.assertTrue(ok)
        policy = client.set_missing_host_key_policy.call_args[0][0]
        key = mock.Mock()
        key.get_name.return_value = "ssh-ed25519"

        # known_hosts に無い接続先で試す。既にある接続先に別の鍵を出すのは
        # 「保存できない」ではなく「鍵の食い違い」で、こちらは接続を中止する
        # （test_known_hosts_key_conflict.py）。ここで見たいのは保存の失敗
        with mock.patch("core.ssh_connection._write_known_hosts_file",
                        side_effect=PermissionError(13, "denied")):
            policy.missing_host_key(client, "192.0.2.2", key)

        self.assertTrue(any("known_hosts" in o for o in output),
                        "保存の失敗を画面に出していない: %s" % output)
        self.assertEqual(errors, [], "警告を接続エラーとして出している")

    def test_a_successful_save_says_nothing(self):
        """対照: 保存できたときは黙っていること。"""
        client = mock.Mock()
        conn, ok, errors, output = self._connect(client)
        policy = client.set_missing_host_key_policy.call_args[0][0]
        key = mock.Mock()
        key.get_name.return_value = "ssh-ed25519"

        # 上と同じ理由で、known_hosts に無い接続先で試す
        policy.missing_host_key(client, "192.0.2.2", key)

        self.assertEqual(output, [])


if __name__ == "__main__":
    unittest.main()
