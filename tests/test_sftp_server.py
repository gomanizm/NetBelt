"""SFTPServerManager (paramiko ベースの自前サーバ) の統合テスト。

ネットワーク機器が繋いでくる状況を模し、素の paramiko をクライアントとして使う。
重点は root_dir の外へ出られないこと（パストラバーサル）と認証。

注意:
  - SFTPServerManager は FTPServerManager と違い、start(port=0) しても self.port に
    実際の待ち受けポートを反映しない。そのためテスト側で空きポートを先に確保して渡す。
  - __init__ が RSAKey.generate(2048) を行い1秒ほどかかるため、クラス単位で使い回す。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
import unittest.mock

import paramiko

sys.path.insert(0, "src")

USER = "netbelt"
PASSWORD = "test-password"


def free_port():
    """空きポートを1つ確保して返す。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SftpServerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

        # 無人テストで実ファイアウォール（UAC/ルール追加）を叩かないようスタブ必須。
        # start() は core.firewall.ensure_inbound_allow を遅延importするため、
        # モジュール属性を直接パッチする。
        cls._fw_patch = unittest.mock.patch(
            "core.firewall.ensure_inbound_allow", return_value=(True, "test stub"))
        cls._fw_patch.start()

        # ホストキーの保存先をユーザーのホームから隔離する
        import tempfile
        from pathlib import Path
        cls._data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        cls._appdir_patch = unittest.mock.patch(
            "core.config_manager.app_data_dir", return_value=cls._data_dir)
        cls._appdir_patch.start()

        cls.root = tempfile.mkdtemp(prefix="netbelt-sftp-")
        cls.port = free_port()

        from core.sftp_server import SFTPServerManager
        cls.server = SFTPServerManager()          # RSA 鍵生成でここが遅い
        started = cls.server.start(port=cls.port, root_dir=cls.root,
                                   username=USER, password=PASSWORD)
        assert started, "SFTP サーバの起動に失敗した"

        deadline = time.time() + 15
        while not cls.server.is_running and time.time() < deadline:
            time.sleep(0.05)
        assert cls.server.is_running, "SFTP サーバが待ち受け状態にならなかった"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._fw_patch.stop()
        cls._appdir_patch.stop()

    def sftp(self, username=USER, password=PASSWORD):
        """テスト用の SFTP セッションを開く。後始末は自動。"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", port=self.port, username=username,
                       password=password, look_for_keys=False,
                       allow_agent=False, timeout=10)
        self.addCleanup(client.close)
        return client.open_sftp()

    def real(self, *parts):
        return os.path.join(self.root, *parts)


class SftpServerAuthTest(SftpServerTestBase):
    def test_correct_credentials_are_accepted(self):
        self.assertIsNotNone(self.sftp())

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(paramiko.AuthenticationException):
            self.sftp(password="wrong-password")

    def test_wrong_username_is_rejected(self):
        with self.assertRaises(paramiko.AuthenticationException):
            self.sftp(username="intruder")


class SftpServerTransferTest(SftpServerTestBase):
    def test_upload_creates_file_under_root(self):
        s = self.sftp()
        with s.open("running-config.txt", "w") as f:
            f.write("hostname R1\n")
        self.assertTrue(os.path.isfile(self.real("running-config.txt")))
        with open(self.real("running-config.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "hostname R1\n")

    def test_download_returns_file_contents(self):
        with open(self.real("image.bin"), "wb") as f:
            f.write(b"\x00\x01\x02NETBELT")
        s = self.sftp()
        with s.open("image.bin", "r") as f:
            self.assertEqual(f.read(), b"\x00\x01\x02NETBELT")

    def test_listdir_shows_uploaded_file(self):
        with open(self.real("listed.cfg"), "w", encoding="utf-8") as f:
            f.write("x")
        self.assertIn("listed.cfg", self.sftp().listdir("."))

    def test_stat_reports_size(self):
        with open(self.real("sized.cfg"), "wb") as f:
            f.write(b"0123456789")
        self.assertEqual(self.sftp().stat("sized.cfg").st_size, 10)

    def test_mkdir_rename_and_rmdir(self):
        s = self.sftp()
        s.mkdir("backup")
        self.assertTrue(os.path.isdir(self.real("backup")))
        s.rename("backup", "backup-old")
        self.assertTrue(os.path.isdir(self.real("backup-old")))
        self.assertFalse(os.path.exists(self.real("backup")))
        s.rmdir("backup-old")
        self.assertFalse(os.path.exists(self.real("backup-old")))

    def test_remove_deletes_file(self):
        with open(self.real("doomed.cfg"), "w", encoding="utf-8") as f:
            f.write("x")
        self.sftp().remove("doomed.cfg")
        self.assertFalse(os.path.exists(self.real("doomed.cfg")))


class SftpServerPathTraversalTest(SftpServerTestBase):
    """root_dir の外へ出られないこと。公開時に最初に見られる箇所。"""

    def _outside(self, name):
        """root_dir の1つ上（＝出てはいけない場所）の実パス。"""
        return os.path.join(os.path.dirname(self.root), name)

    def test_write_above_root_is_denied(self):
        target = self._outside("escaped-write.txt")
        self.addCleanup(lambda: os.path.exists(target) and os.remove(target))
        s = self.sftp()
        with self.assertRaises(IOError):
            with s.open("../escaped-write.txt", "w") as f:
                f.write("owned")
        self.assertFalse(os.path.exists(target), "root_dir の外にファイルが作られた")

    def test_deep_traversal_write_is_denied(self):
        s = self.sftp()
        with self.assertRaises(IOError):
            with s.open("../../../../escaped-deep.txt", "w") as f:
                f.write("owned")

    def test_absolute_path_is_confined_to_root(self):
        # 絶対パスは root_dir 配下として解釈される（chroot 相当）
        s = self.sftp()
        with s.open("/confined.txt", "w") as f:
            f.write("ok")
        self.assertTrue(os.path.isfile(self.real("confined.txt")))

    def test_read_above_root_is_denied(self):
        secret = self._outside("secret-outside.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("classified")
        self.addCleanup(os.remove, secret)
        with self.assertRaises(IOError):
            self.sftp().open("../secret-outside.txt", "r")

    def test_listdir_above_root_is_denied(self):
        with self.assertRaises(IOError):
            self.sftp().listdir("..")

    def test_stat_above_root_is_denied(self):
        secret = self._outside("secret-stat.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("classified")
        self.addCleanup(os.remove, secret)
        with self.assertRaises(IOError):
            self.sftp().stat("../secret-stat.txt")

    def test_remove_above_root_is_denied(self):
        victim = self._outside("victim.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("do not delete")
        self.addCleanup(os.remove, victim)
        with self.assertRaises(IOError):
            self.sftp().remove("../victim.txt")
        self.assertTrue(os.path.exists(victim), "root_dir の外のファイルが消された")

    def test_rename_out_of_root_is_denied(self):
        with open(self.real("inside.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        target = self._outside("moved-out.txt")
        self.addCleanup(lambda: os.path.exists(target) and os.remove(target))
        with self.assertRaises(IOError):
            self.sftp().rename("inside.txt", "../moved-out.txt")
        self.assertFalse(os.path.exists(target), "root_dir の外へ移動できてしまった")

    def test_symlink_escape_is_denied(self):
        """root_dir 内のシンボリックリンク経由で外へ出られないこと。

        _get_real_path は abspath のみでリンクを解決しないため、リンクを辿れてしまうと
        chroot が破れる。Windows ではリンク作成に権限が要るので、作れなければスキップ。
        """
        secret = self._outside("secret-symlink.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("classified")
        self.addCleanup(os.remove, secret)

        link = self.real("link-to-outside.txt")
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError, AttributeError) as e:
            self.skipTest("シンボリックリンクを作成できない環境: %s" % e)
        self.addCleanup(lambda: os.path.exists(link) and os.remove(link))

        with self.assertRaises(IOError):
            self.sftp().open("link-to-outside.txt", "r")


class SftpServerLifecycleTest(unittest.TestCase):
    """起動・停止まわり。サーバを個別に立てるためベースクラスとは分ける。"""

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        patch = unittest.mock.patch(
            "core.firewall.ensure_inbound_allow", return_value=(True, "test stub"))
        patch.start()
        self.addCleanup(patch.stop)
        # ホストキーの保存先をユーザーのホームから隔離する
        from pathlib import Path
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = unittest.mock.patch("core.config_manager.app_data_dir",
                                 return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)

    def test_start_creates_missing_root_directory(self):
        from core.sftp_server import SFTPServerManager
        base = tempfile.mkdtemp(prefix="netbelt-sftp-life-")
        root = os.path.join(base, "not-yet-created")
        self.assertFalse(os.path.exists(root))

        m = SFTPServerManager()
        self.addCleanup(m.stop)
        self.assertTrue(m.start(port=free_port(), root_dir=root,
                                username=USER, password=PASSWORD))
        self.assertTrue(os.path.isdir(root), "root_dir が自動作成されなかった")

    def test_second_start_is_rejected_while_running(self):
        from core.sftp_server import SFTPServerManager
        root = tempfile.mkdtemp(prefix="netbelt-sftp-life-")
        m = SFTPServerManager()
        self.addCleanup(m.stop)
        self.assertTrue(m.start(port=free_port(), root_dir=root,
                                username=USER, password=PASSWORD))
        deadline = time.time() + 15
        while not m.is_running and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(m.is_running)

        errors = []
        m.error_occurred.connect(errors.append)
        self.assertFalse(m.start(port=free_port(), root_dir=root,
                                 username=USER, password=PASSWORD))

    def test_empty_credentials_are_rejected(self):
        """空のユーザー名/パスワードでネットワークに晒さないこと。

        UI 側でも検証しているが、UI を経由しない呼び出しに備えて start() 自身でも拒否する。
        """
        from core.sftp_server import SFTPServerManager
        root = tempfile.mkdtemp(prefix="netbelt-sftp-life-")
        for username, password in (("", ""), (USER, ""), ("", PASSWORD)):
            with self.subTest(username=username, password=password):
                m = SFTPServerManager()
                self.addCleanup(m.stop)
                errors = []
                m.error_occurred.connect(errors.append)
                self.assertFalse(m.start(port=free_port(), root_dir=root,
                                         username=username, password=password))
                self.assertFalse(m.is_running)
                self.assertTrue(errors, "error_occurred が出ていない")

    def test_stop_marks_server_not_running(self):
        from core.sftp_server import SFTPServerManager
        root = tempfile.mkdtemp(prefix="netbelt-sftp-life-")
        m = SFTPServerManager()
        m.start(port=free_port(), root_dir=root, username=USER, password=PASSWORD)
        deadline = time.time() + 15
        while not m.is_running and time.time() < deadline:
            time.sleep(0.05)
        m.stop()
        self.assertFalse(m.is_running)


if __name__ == "__main__":
    unittest.main()
