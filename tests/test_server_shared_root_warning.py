"""FTP / SFTP / TFTP のルートが同じ・入れ子のとき、起動時に警告すること（断らない）。

実測（基準 441ea02、127.0.0.1 で 3 つのサーバーを同じルートで起動）: FTP の
A が same.cfg へ STOR して b'AAAA' を送り、データ接続を開いたまま止める。
同じプロトコルの B は 450 File busy で断られるのに、SFTP で同じ名前へ
b'S'*20 を書くと write+close が通り、TFTP の WRQ で b'T'*30 を送ると ACK 1
で受理された。最後に A が b'aaaaaaaa' を送ると 226 Transfer complete で、
最終内容は b'AAAAaaaaaaaaTTTTTTTTTTTTTTTTTT'（3 本とも成功と報告されたまま
中身が混ざる）。書き込み中の保存先の予約は FTPServerManager._uploads・
SFTPServerManager._open_writers・TFTPServer._wrq_targets とサーバーごとに
別で、互いを見ないのが原因。起動の時点でも何も知らせていなかった。

直し方（利用者の決定 B）: 共通の予約台帳へ直すのは 1.3.2 の範囲を超えるので、
起動は断らずに、起動したサーバーのルートが動いている他のサーバーのルートと
同じか入れ子なら、起動したパネルのログへ警告を出す（MainWindow が 3 つの
マネージャの started を受けて比べる）。比べるのは realpath → normcase した
パス（予約の鍵と同じ考え方）。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 警告の目印（文言の要点）
WARN_MARK = "混ざる"


def _free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SharedServerRootWarningTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()
        cls._keep.clear()

    def setUp(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        self.base = tempfile.mkdtemp(prefix="netbelt-shared-root-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.base, "config.json"))
            self.w = MainWindow()
        self._keep.append(self.w)
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for panel, server in ((self.w.ftp_server_panel, self.w.ftp_server_panel.ftp_server),
                              (self.w.tftp_server_panel, self.w.tftp_server_panel.tftp_server),
                              (self.w.sftp_server_panel, self.w.sftp_server_panel.sftp_server)):
            if server.is_running:
                panel._on_stop_server()
        self.app.processEvents()

    def _dir(self, *parts):
        path = os.path.join(self.base, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    # --- 各サーバーをパネル自身の経路で起動する -------------------------

    def _start_ftp(self, root):
        panel = self.w.ftp_server_panel
        panel.port_spin.setValue(_free_tcp_port())
        panel.root_dir_edit.setText(root)
        panel.anonymous_check.setChecked(False)
        panel.username_edit.setText("tester")
        panel.password_edit.setText("example-pass")
        panel._on_start_server()
        self.assertTrue(panel.ftp_server.is_running, "FTP の起動を断っている")
        return panel

    def _start_tftp(self, root):
        panel = self.w.tftp_server_panel
        panel.port_spin.setValue(_free_udp_port())
        panel.root_dir_edit.setText(root)
        panel._on_start_server()
        self.assertTrue(panel.tftp_server.is_running, "TFTP の起動を断っている")
        return panel

    def _start_sftp(self, root):
        import paramiko
        panel = self.w.sftp_server_panel
        # 鍵の生成と保存を避ける（起動の経路はパネルのまま）
        panel.sftp_server.host_key = paramiko.RSAKey.generate(1024)
        panel.port_spin.setValue(_free_tcp_port())
        panel.root_dir_edit.setText(root)
        panel.username_edit.setText("tester")
        panel.password_edit.setText("example-pass")
        panel._on_start_server()
        # started は待受スレッドから飛ぶ（queued）。届くまで回す
        deadline = time.monotonic() + 10
        while "サーバー起動" not in self._log(panel) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertIn("サーバー起動", self._log(panel), "SFTP が起動していない")
        self.app.processEvents()
        return panel

    @staticmethod
    def _log(panel):
        return panel.log_text.toPlainText()

    # --- 警告を出す -----------------------------------------------------

    def test_same_root_as_a_running_tftp_is_warned_on_ftp_start(self):
        shared = self._dir("shared")
        self._start_tftp(shared)
        # 綴りが違っても同じフォルダ（大文字小文字・末尾の区切り）
        ftp = self._start_ftp(shared.upper() + os.sep)
        log = self._log(ftp)
        self.assertIn(WARN_MARK, log, "同じルートなのに警告していない:\n%s" % log)
        self.assertIn("TFTP", log, "どのサーバーと重なっているかが出ていない:\n%s" % log)

    def test_a_root_inside_a_running_server_root_is_warned(self):
        outer = self._dir("outer")
        self._start_tftp(outer)
        ftp = self._start_ftp(self._dir("outer", "configs"))
        self.assertIn(WARN_MARK, self._log(ftp))

    def test_a_root_containing_a_running_server_root_is_warned(self):
        outer = self._dir("outer")
        self._start_ftp(self._dir("outer", "configs"))
        tftp = self._start_tftp(outer)
        log = self._log(tftp)
        self.assertIn(WARN_MARK, log)
        self.assertIn("FTP", log)

    def test_sftp_started_later_is_warned_too(self):
        """SFTP の started は待受スレッドから届く（queued）。それでも警告する"""
        shared = self._dir("shared")
        self._start_ftp(shared)
        sftp = self._start_sftp(shared)
        log = self._log(sftp)
        self.assertIn(WARN_MARK, log, "SFTP の起動で警告していない:\n%s" % log)
        self.assertIn("FTP", log)

    # --- 警告を出さない -------------------------------------------------

    def test_separate_roots_are_not_warned(self):
        # 名前の前方一致（root と root2）は入れ子ではない
        self._start_tftp(self._dir("root"))
        ftp = self._start_ftp(self._dir("root2"))
        self.assertNotIn(WARN_MARK, self._log(ftp))

    def test_a_stopped_server_with_the_same_root_is_not_warned(self):
        shared = self._dir("shared")
        # TFTP の欄は同じルートだが、起動していない
        self.w.tftp_server_panel.root_dir_edit.setText(shared)
        ftp = self._start_ftp(shared)
        self.assertNotIn(WARN_MARK, self._log(ftp))


if __name__ == "__main__":
    unittest.main()
