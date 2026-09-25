"""機器が SFTP のチャンネルだけを閉じたとき、次の操作の失敗で接続を畳むことを検証する。

実測（基準 097550c、src/core/sftp_manager.py:114-143）:
  機器が SFTP サブシステムのチャンネルだけを閉じると（sftp-server が
  落ちたのと同じ。SSH のトランスポートは生きている）、paramiko は以後の
  操作で OSError('Socket is closed') を上げる。_fail は切断を例外の型
  （_DROPPED_CONNECTION_ERRORS）だけで見分けていたので、これを普通の失敗と
  して『ディレクトリ一覧取得エラー: Socket is closed』を出すだけで畳まず、
    * is_connected=True のまま、disconnected も出ない
    * パネルは DROPPED_TEXT を出さない。行を選んで削除すると確認ダイアログ
      まで開き、Yes のあとで『削除エラー: Socket is closed』になる
  （localhost の実 paramiko サーバで再現）。以後の操作はすべて同じ失敗を
  繰り返すだけで、機器へ繋ぎ直す以外に戻す道が無いことも分からない。

利用者の決定:
  2026-09-20: 切断・壊れた応答は、期限切れと同じく接続を畳む。
  2026-09-23: SFTP のチャンネルだけが死んだら、一覧は残し、操作は断る。

直し方:
  _fail で、型では切断と分からない失敗でも、接続中のままで SFTP の
  チャンネルが閉じていれば（get_channel().closed が True そのもの）切断と
  同じく畳む。接続中であることも条件にするのは、畳んだあとにロックを
  待っていた操作が出す『SFTP接続がありません』に『切断しました』を付けて、
  disconnected を重ねて出さないため。開いているチャンネルでの失敗は、
  これまでどおり理由を出すだけで畳まない。
"""
import os
import posixpath
import socket
import stat
import sys
import threading
import time
import unittest
from unittest import mock

import paramiko
from paramiko import SFTPServer

import test_sftp_upload_tmp_mode as base   # _MemoryFS / _SftpInterface / _Auth

sys.path.insert(0, "src")

DROPPED_SUFFIX = "。SFTP接続を切断しました。接続し直してください"


class FakeAttr:
    """listdir_attr が返す 1 項目分"""

    def __init__(self, filename, st_mode, st_size=1024, st_mtime=1700000000):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = st_size
        self.st_mtime = st_mtime


class _CapturingSFTPServer(SFTPServer):
    """機器側から閉じられるよう、SFTP のチャンネルを控えておく"""

    channels = []

    def __init__(self, channel, name, server, sftp_si, *args, **kwargs):
        super().__init__(channel, name, server, sftp_si, *args, **kwargs)
        type(self).channels.append(channel)


class _Server:
    """localhost の SSH サーバ。SFTP サブシステムで _MemoryFS を出す"""

    def __init__(self, fs):
        self.fs = fs
        self.key = paramiko.ECDSAKey.generate()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.key)
            t.set_subsystem_handler("sftp", _CapturingSFTPServer,
                                    base._SftpInterface, self.fs)
            t.start_server(server=base._Auth())
            self.transports.append(t)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for t in list(self.transports):
            t.close()


class SftpClosedChannelFoldsTest(unittest.TestCase):
    _panels = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # offscreen では誰もモーダルを閉じられない
        for name in ("critical", "warning", "information"):
            patcher = mock.patch("ui.sftp_panel.QMessageBox." + name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _pump(self, check, timeout=5.0):
        from PyQt6.QtWidgets import QApplication
        deadline = time.time() + timeout
        while time.time() < deadline:
            QApplication.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _settle_and_detach(self, panel):
        """モーダルの差し替えが効いているうちに残りの通知を配り、パネルを外す。

        スレッドから届く通知が残ったままテストを抜けると、セッションの
        終わりに本物の QMessageBox が開き、offscreen では誰も閉じられずに
        止まる（実測）。
        """
        self._pump(lambda: False, 0.3)
        panel.clear()

    def _watch(self, m):
        self.errors, self.dropped = [], []
        m.error_occurred.connect(self.errors.append)
        m.disconnected.connect(lambda: self.dropped.append(True))

    def _mock_manager(self, closed):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        m.current_path = "/flash"
        m.sftp_client = mock.Mock()
        m.sftp_client.get_channel.return_value.closed = closed
        self._watch(m)
        return m

    def test_a_failure_on_a_closed_channel_folds_the_connection(self):
        """チャンネルが閉じていたら、型が OSError でも畳むこと。"""
        m = self._mock_manager(closed=True)
        m.sftp_client.listdir_attr.side_effect = OSError("Socket is closed")

        m.list_directory()
        # 通知は一覧のスレッドから届く。畳むのは通知を出したあと（同じ
        # スレッド）なので、失敗が届いただけで判定すると早すぎる
        self.assertTrue(self._pump(lambda: bool(self.errors)),
                        "前提: 一覧の失敗が届かない")
        self._pump(lambda: bool(self.dropped))

        self.assertEqual(self.errors, [
            "ディレクトリ一覧取得エラー: Socket is closed" + DROPPED_SUFFIX])
        self.assertFalse(m.is_connected, "閉じたチャンネルを接続中のまま残した")
        self.assertIsNone(m.sftp_client, "使えないチャンネルを掴んだまま")
        self.assertEqual(self.dropped, [True], "切断を知らせていない")

    def test_the_panel_refuses_operations_once_the_device_closed_the_channel(self):
        """実 paramiko: 機器がチャンネルを閉じたあとは、確認を開かずに断ること。"""
        from core.sftp_manager import SFTPManager
        from ui import sftp_panel as mod
        from ui.sftp_panel import SFTPPanel

        fs = base._MemoryFS()
        fs.files[base.HOME + "/boot.bin"] = base._Node(0o644, b"x" * 100)
        fs.files[base.HOME + "/old.cfg"] = base._Node(0o644, b"y" * 10)
        server = _Server(fs)
        self.addCleanup(server.close)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", port=server.port, username=base.USER,
                    password=base.PASSWORD, look_for_keys=False,
                    allow_agent=False, timeout=10)
        self.addCleanup(ssh.close)
        m = SFTPManager()
        self.assertTrue(m.connect(ssh), "前提: SFTP に接続できない")
        self.addCleanup(m.disconnect)
        self._watch(m)
        panel = SFTPPanel()
        type(self)._panels.append(panel)      # 親より先に捨てない
        self.addCleanup(self._settle_and_detach, panel)
        panel.set_sftp_manager(m, "router-A", "192.0.2.10")
        self.assertTrue(self._pump(lambda: panel.model.rowCount() == 2),
                        "前提: 一覧が 2 行出ていること")

        # 機器側で SFTP のチャンネルだけを閉じる（SSH は生きている）
        _CapturingSFTPServer.channels[-1].close()
        self.assertTrue(self._pump(lambda: m.sftp_client.get_channel().closed),
                        "前提: 閉じた通知が届かない")

        panel._on_refresh()
        self.assertTrue(self._pump(lambda: bool(self.errors)),
                        "前提: 一覧の失敗が届かない")
        # 畳んだ知らせ（スレッドから届く）をパネルが受け取るまで待つ
        self._pump(lambda: panel.status_label.text()
                   == mod.SFTPPanel.DROPPED_TEXT)

        self.assertFalse(m.is_connected, "閉じたチャンネルを接続中のまま残した")
        self.assertEqual(self.dropped, [True], "切断を知らせていない")
        self.assertEqual(panel.model.rowCount(), 2, "一覧まで消えた")
        self.assertEqual(panel.status_label.text(), mod.SFTPPanel.DROPPED_TEXT)
        with mock.patch.object(mod.QMessageBox, "question") as question:
            panel._on_delete_selected({"name": "old.cfg", "is_dir": False})
        question.assert_not_called()
        self.assertTrue(ssh.get_transport().is_active(),
                        "SSH のセッションまで閉じた（端末側のもの）")

    def test_a_failure_on_an_open_channel_does_not_fold(self):
        """対照: 開いているチャンネルでの失敗は、理由を出すだけで畳まないこと。"""
        m = self._mock_manager(closed=False)
        m.sftp_client.remove.side_effect = OSError("Permission denied")

        m.delete_item("/flash/boot.bin")

        self.assertEqual(self.errors, ["削除エラー: Permission denied"])
        self.assertTrue(m.is_connected, "開いているチャンネルで接続ごと畳んだ")
        self.assertEqual(self.dropped, [])

    def test_a_late_failure_after_the_fold_does_not_fold_again(self):
        """対照: 畳んだあとの『SFTP接続がありません』で、切断を重ねて出さないこと。

        disconnect は is_connected を先に落としてからロックを待つので、
        ロックの中で確かめた操作が失敗を返す時点では、まだ閉じたチャンネルを
        掴んでいることがある。
        """
        m = self._mock_manager(closed=True)
        m.is_connected = False

        m._fail("ディレクトリ一覧取得エラー", IOError("SFTP接続がありません"))

        self.assertEqual(self.errors,
                         ["ディレクトリ一覧取得エラー: SFTP接続がありません"])
        self.assertEqual(self.dropped, [], "畳んだあとに切断を重ねて知らせた")


if __name__ == "__main__":
    unittest.main()
