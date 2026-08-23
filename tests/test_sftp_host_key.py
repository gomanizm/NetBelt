"""SFTP サーバのホストキーが永続化されることを検証する回帰テスト。

起動のたびに生成すると毎回ホストキーが変わり、一度接続したクライアント
（WinSCP / OpenSSH / ネットワーク機器）は次回から host key mismatch で
接続を拒否する。結果、SFTP サーバが実質2回目以降使えなくなる。

また鍵生成は 2048bit RSA で1秒前後かかるため、__init__ ではなく start() の
時点で用意する（アプリ起動を待たせない）。
"""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import paramiko

sys.path.insert(0, "src")


class SftpHostKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # ユーザーのホームを汚さないよう、データ保存先を一時ディレクトリへ差し替える
        self.data_dir = Path(tempfile.mkdtemp(prefix="netbelt-hostkey-"))
        patch = unittest.mock.patch("core.config_manager.app_data_dir",
                                    return_value=self.data_dir)
        patch.start()
        self.addCleanup(patch.stop)
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _manager(self):
        from core.sftp_server import SFTPServerManager
        return SFTPServerManager()

    def _fingerprint(self, key):
        import hashlib
        return hashlib.sha256(key.asbytes()).hexdigest()

    def test_init_does_not_generate_key(self):
        """__init__ では鍵を作らない（アプリ起動を待たせないため）。"""
        m = self._manager()
        self.assertIsNone(m.host_key, "__init__ でホストキーが生成されている")

    def test_key_is_persisted_to_disk(self):
        m = self._manager()
        key = m._load_or_create_host_key()
        self.assertIsNotNone(key)
        self.assertTrue((self.data_dir / "sftp_host_key").is_file(),
                        "ホストキーがファイルへ保存されていない")

    def test_same_key_across_instances(self):
        """別インスタンスでも同じホストキーになること（本件の核心）。"""
        first = self._manager()._load_or_create_host_key()
        second = self._manager()._load_or_create_host_key()
        self.assertEqual(self._fingerprint(first), self._fingerprint(second),
                         "インスタンスごとにホストキーが変わっている")

    def test_key_survives_reload_from_file(self):
        """保存された鍵をファイルから読み直しても同一であること。"""
        original = self._manager()._load_or_create_host_key()
        loaded = paramiko.RSAKey(filename=str(self.data_dir / "sftp_host_key"))
        self.assertEqual(self._fingerprint(original), self._fingerprint(loaded))

    def test_corrupted_key_file_is_regenerated(self):
        """鍵ファイルが壊れていても起動できること（再生成にフォールバック）。"""
        (self.data_dir / "sftp_host_key").write_text("not a key", encoding="utf-8")
        key = self._manager()._load_or_create_host_key()
        self.assertIsNotNone(key, "壊れた鍵ファイルから復帰できていない")

    def test_client_sees_same_key_on_restart(self):
        """サーバを立て直しても、クライアントから見た鍵が変わらないこと。"""
        import socket

        def free_port():
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
            s.close()
            return p

        seen = []
        for _ in range(2):
            m = self._manager()
            port = free_port()
            root = tempfile.mkdtemp(prefix="netbelt-hostkey-root-")
            self.assertTrue(m.start(port=port, root_dir=root,
                                    username="u", password="p"))
            deadline = time.time() + 15
            while not m.is_running and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(m.is_running)

            t = paramiko.Transport(("127.0.0.1", port))
            t.start_client(timeout=10)
            seen.append(self._fingerprint(t.get_remote_server_key()))
            t.close()
            m.stop()

        self.assertEqual(seen[0], seen[1],
                         "再起動でホストキーが変わり、クライアントは接続を拒否する")


if __name__ == "__main__":
    unittest.main()
