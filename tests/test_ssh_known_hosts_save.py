"""known_hosts の保存が、保存済みのホスト鍵を消さないことを検証する。

paramiko 4.0.0 の SSHClient.save_host_keys は保存先を "w" で開いて先に
切り詰めてから書く。その直前の再読込は load_host_keys 済み
(self._host_keys_filename is not None) のときしか走らない。
_setup_host_keys は known_hosts が存在するときしか load_host_keys を
呼ばないので、ファイルがまだ無い時点で始めた接続の client は再読込の
対象を持たない。その状態で 2 台に初回接続すると、あとから保存した側が
先に保存された鍵を消す。消された機器は次回また「未知」に戻り、
_TofuHostKeyPolicy が何も聞かずに新しい鍵を受け入れる。

切り詰め書き込みそのものも危うい。書いている途中で落ちると（ディスク
満杯・強制終了）、保存済みの鍵をまとめて失う。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402


class KnownHostsSaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="netbelt-khsave-"))
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=self.data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.known_hosts = self.data_dir / "known_hosts"

    def _tofu(self):
        """_setup_host_keys を通した本物の SSHClient とその TOFU ポリシー。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        client = paramiko.SSHClient()
        conn._setup_host_keys(client)
        return conn, client, client._policy

    def test_saving_a_second_host_keeps_the_first(self):
        """ファイルが無い時点で始めた 2 接続が、互いの鍵を消さないこと。"""
        key_a = paramiko.ECDSAKey.generate()
        key_b = paramiko.ECDSAKey.generate()
        _, client_a, policy_a = self._tofu()
        _, client_b, policy_b = self._tofu()

        policy_a.missing_host_key(client_a, "192.0.2.1", key_a)
        policy_b.missing_host_key(client_b, "192.0.2.2", key_b)

        saved = paramiko.HostKeys(str(self.known_hosts))
        self.assertIsNotNone(saved.lookup("192.0.2.2"),
                             "あとから保存した鍵が入っていない")
        self.assertIsNotNone(
            saved.lookup("192.0.2.1"),
            "先に保存された鍵が消えている: %r"
            % self.known_hosts.read_text(encoding="utf-8"))

    def test_a_failed_save_leaves_the_previous_file_intact(self):
        """保存の途中で落ちても、保存済みの鍵を失わないこと。"""
        _, client_a, policy_a = self._tofu()
        policy_a.missing_host_key(client_a, "192.0.2.1",
                                  paramiko.ECDSAKey.generate())
        before = self.known_hosts.read_text(encoding="utf-8")

        _, client_b, policy_b = self._tofu()

        def write_then_fail(filename):
            """途中まで書いたところで力尽きる保存。"""
            with open(filename, "w", encoding="utf-8") as f:
                f.write("192.0.2.2 ecdsa-sha2-nistp256 AAAA")
            raise OSError(28, "No space left on device")

        with mock.patch.object(client_b, "save_host_keys", write_then_fail):
            policy_b.missing_host_key(client_b, "192.0.2.2",
                                      paramiko.ECDSAKey.generate())

        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"), before,
                         "保存に失敗したのに保存済みのファイルが壊れている")


if __name__ == "__main__":
    unittest.main()
