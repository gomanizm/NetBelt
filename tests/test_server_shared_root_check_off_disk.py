"""サーバー起動時のルートの重なり確認が、動いている別サーバーのルートのディスクに触れないことを検証する。

何が起きていたか（b2858c4 で実測。共有フォルダの代役として、ntpath の
_getfinalpathname のうち FTP のルート配下への呼び出しだけを差し替えた）。
_warn_shared_server_root は、起動したサーバーと動いている別サーバーのルートを
os.path.realpath → normcase で比べていた。realpath はルートを開いて最終パスを
問い合わせる（1 回の realpath で _getfinalpathname を 2 回）。これが GUI
スレッドで走るので:
  - FTP のルートが応答しない共有（1 回 2 秒の代役）だと、TFTP を起動した
    だけで GUI が 4.00 秒止まった
  - 切れた共有（WinError 64 / 1231 / 121。realpath が飲む番号に無い）だと
    OSError がスロットの外へ出て「予期しないエラーが発生しました」の
    ダイアログになった。確認が途中で抜けるので、その先にある別サーバーとの
    重なりの警告も出なかった
441ea02 にはこの確認が無い（1.3.2 で入った）。

どう直したか。ディスクに触れずに比べる（abspath → normcase。abspath は文字列と
カレントディレクトリだけで絶対パスにする）。確認は警告だけなので、
ジャンクションやシンボリックリンクを通した同じフォルダまでは追わない。
"""
import ntpath
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 警告の目印（文言の要点）
WARN_MARK = "混ざる"
# 切れた共有で Windows が返す番号（ERROR_NETNAME_DELETED）
WINERROR_NETNAME_DELETED = 64


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


class SharedRootCheckOffDiskTest(unittest.TestCase):
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
        self.base = tempfile.mkdtemp(prefix="netbelt-root-off-disk-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.base, "config.json"))
            self.w = MainWindow()
        self._keep.append(self.w)
        self.addCleanup(self._stop_all)
        # スロットの外へ出た例外（本番では「予期しないエラー」のダイアログ）
        self.escaped = []
        patcher = mock.patch("sys.excepthook", lambda *exc: self.escaped.append(
            "".join(traceback.format_exception(*exc))))
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def _start_ftp(self, root):
        panel = self.w.ftp_server_panel
        panel.port_spin.setValue(_free_tcp_port())
        panel.root_dir_edit.setText(root)
        panel.anonymous_check.setChecked(False)
        panel.username_edit.setText("tester")
        panel.password_edit.setText("example-pass")
        panel._on_start_server()
        self.assertTrue(panel.ftp_server.is_running, "前提: FTP が起動していない")
        self.app.processEvents()
        return panel

    def _start_tftp(self, root):
        panel = self.w.tftp_server_panel
        panel.port_spin.setValue(_free_udp_port())
        panel.root_dir_edit.setText(root)
        panel._on_start_server()
        self.app.processEvents()
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
        while ("サーバー起動" not in panel.log_text.toPlainText()
               and time.monotonic() < deadline):
            self.app.processEvents()
            time.sleep(0.02)
        self.assertIn("サーバー起動", panel.log_text.toPlainText(),
                      "前提: SFTP が起動していない")
        self.app.processEvents()
        return panel

    def _share_is_gone(self, share):
        """share の配下を GUI スレッドで開こうとすると、切れた共有と同じ OSError にする

        GUI スレッドから share の配下へ触れた回数を返すリストを返す。
        """
        stalled = os.path.normcase(os.path.abspath(share))
        touched = []

        def wrap(real):
            def fake(path, *args, **kwargs):
                p = os.path.normcase(os.fspath(path))
                if p.startswith("\\\\?\\"):
                    p = p[4:]
                if (p.startswith(stalled)
                        and threading.current_thread() is threading.main_thread()):
                    touched.append(p)
                    raise OSError(0, "指定されたネットワーク名は利用できなくなりました。",
                                  os.fspath(path), WINERROR_NETNAME_DELETED)
                return real(path, *args, **kwargs)
            return fake

        for owner, name in ((ntpath, "_getfinalpathname"), (os, "stat"), (os, "lstat")):
            real = getattr(owner, name, None)
            if real is None:
                continue
            patcher = mock.patch.object(owner, name, wrap(real))
            patcher.start()
            self.addCleanup(patcher.stop)
        return touched

    def test_starting_a_server_does_not_touch_a_running_server_root(self):
        share = self._dir("share-root")
        self._start_ftp(share)
        touched = self._share_is_gone(share)

        tftp = self._start_tftp(self._dir("local-root"))

        self.assertEqual([], touched,
                         "動いている FTP のルートに GUI スレッドで触れた"
                         "（応答しない共有では固まる）")
        self.assertEqual([], self.escaped,
                         "重なりの確認の例外がスロットの外へ出た（予期しないエラー）")
        self.assertTrue(tftp.tftp_server.is_running)
        self.assertNotIn(WARN_MARK, tftp.log_text.toPlainText())

    def test_a_vanished_share_does_not_hide_an_overlap_with_another_server(self):
        share = self._dir("share-root")
        local = self._dir("local-root")
        self._start_ftp(share)
        self._start_sftp(local)
        self._share_is_gone(share)

        tftp = self._start_tftp(local)

        log = tftp.log_text.toPlainText()
        self.assertIn(WARN_MARK, log,
                      "FTP のルートを確かめられず、SFTP との重なりを警告しなかった:\n%s" % log)
        self.assertIn("SFTP", log)
        self.assertEqual([], self.escaped)


if __name__ == "__main__":
    unittest.main()
