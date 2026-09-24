"""リンクの確認で SFTP のチャンネルが閉じていたら、接続を畳むことを検証する。

何が起きていたか（基準 470c538 での実測。localhost の実 paramiko サーバ）:
  機器が SFTP のチャンネルだけを閉じると（SSH のトランスポートは生きて
  いる）、paramiko は以後の要求で OSError('Socket is closed') を上げる。
  _fail はこれをチャンネルの状態で見分けて畳むが、リンクを確認する 3 か所は
  例外を自分で受けていて _fail へ届かなかった。

  (a) 一覧のリンク先 stat — listdir_attr のあとでチャンネルが閉じると、
      リンクの stat の失敗を壊れたリンクと同じく continue で捨て、
      file_list_ready が「取得成功」として届いた（ディレクトリへのリンクは
      ファイル表示で入れない）。errors=[] / is_connected=True /
      disconnected 0 回。
  (b) inspect_link_target の readlink — 名前が読めなかっただけとして
      (16877, None) を返し、リンク先の名前なしで権限変更のダイアログへ
      進む。errors=[] / is_connected=True。
  (c) inspect_link_target の stat（権限変更の前）— 待機中に機器が閉じて
      いると最初の要求で落ちるが、"'link-to-dir' のリンク先を読めないため、
      パーミッションを変更できません（Socket is closed）" と壊れたリンクと
      同じ文面を出すだけで接続が残った。パネルは「SFTP は切断されました」を
      出さず、次に削除を選ぶと確認ダイアログまで開いた（利用者の決定
      2026-09-23 が避けたかった流れ）。

利用者の決定:
  2026-09-20: 切断・壊れた応答は、期限切れと同じく接続を畳む。
  2026-09-23: SFTP のチャンネルだけが死んだら、接続を畳んで「SFTP は切断
  されました」と知らせ、操作は断る。一覧の表示は残す。

どう直したか:
  3 か所とも、失敗した時点でチャンネルが閉じていれば（_channel_closed）
  切断と同じ扱いにして _fail へ回す。(a) はリンクの stat を raise して
  外側の _fail に畳ませ、(b) は readlink を raise し、(c) は stat の失敗を
  「リンク先を読めない」ではなく _fail へ渡す。開いているチャンネルでの
  失敗（壊れたリンク・readlink に応じない機器）は、これまでどおり。
"""
import os
import posixpath
import socket
import stat as stat_mod
import sys
import threading
import time
import unittest
from unittest import mock

import paramiko
from paramiko import SFTPAttributes, SFTPServer, SFTP_NO_SUCH_FILE

import test_sftp_upload_tmp_mode as base   # _MemoryFS / _SftpInterface / _Auth

sys.path.insert(0, "src")

DROPPED_SUFFIX = "。SFTP接続を切断しました。接続し直してください"
CLOSED = "Socket is closed"
LINK = base.HOME + "/link-to-dir"


class _Attr:
    """listdir_attr / stat が返す項目の代わり（必要な属性だけ持つ）"""

    def __init__(self, filename, st_mode, st_size=0, st_mtime=1700000000):
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


def _server_attr(name, mode):
    a = SFTPAttributes()
    a.st_mode = mode
    a.st_size = 0
    a.st_uid = a.st_gid = 1000
    a.st_atime = a.st_mtime = 0
    a.filename = name
    return a


class _LinkInterface(base._SftpInterface):
    """ホームに cfg-dir（ディレクトリ）と link-to-dir -> cfg-dir を足す"""

    def list_folder(self, path):
        out = super().list_folder(path)
        if self.fs.norm(path) == base.HOME:
            out = out + [_server_attr("cfg-dir", stat_mod.S_IFDIR | 0o755),
                         _server_attr("link-to-dir", stat_mod.S_IFLNK | 0o777)]
        return out

    def stat(self, path):
        p = self.fs.norm(path)
        if p in (base.HOME + "/cfg-dir", LINK):
            return _server_attr(posixpath.basename(p), stat_mod.S_IFDIR | 0o755)
        return super().stat(path)

    def lstat(self, path):
        if self.fs.norm(path) == LINK:
            return _server_attr("link-to-dir", stat_mod.S_IFLNK | 0o777)
        return self.stat(path)

    def readlink(self, path):
        if self.fs.norm(path) == LINK:
            return "cfg-dir"
        return SFTP_NO_SUCH_FILE


class _Server:
    """localhost の SSH サーバ。SFTP サブシステムでリンク入りのホームを出す"""

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
                                    _LinkInterface, self.fs)
            t.start_server(server=base._Auth())
            self.transports.append(t)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for t in list(self.transports):
            t.close()


class _Base(unittest.TestCase):
    _panels = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # offscreen では誰もモーダルを閉じられない
        self.warning = mock.patch("ui.sftp_panel.QMessageBox.warning").start()
        for name in ("critical", "information"):
            mock.patch("ui.sftp_panel.QMessageBox." + name).start()
        self.addCleanup(mock.patch.stopall)

    def _pump(self, check, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _watch(self, m):
        self.errors, self.gone, self.lists = [], [], []
        m.error_occurred.connect(self.errors.append)
        m.disconnected.connect(lambda: self.gone.append(True))
        # テストのあとに届く知らせが、GC で空にされた lambda を呼ばないよう外す
        self.addCleanup(m.disconnected.disconnect)
        m.file_list_ready.connect(self.lists.append)

    def _assert_folded_once(self, m, prefix):
        """畳んだうえで、切断の知らせを 1 回だけ出したこと"""
        self.assertEqual(self.errors, [prefix + ": " + CLOSED + DROPPED_SUFFIX],
                         "切断の知らせになっていない・重なっている")
        self.assertFalse(m.is_connected, "閉じたチャンネルを接続中のまま残した")
        self.assertIsNone(m.sftp_client, "使えないチャンネルを掴んだまま")
        self.assertEqual(self.gone, [True], "disconnected は 1 回であること")


class ClosedChannelLinkChecksMockTest(_Base):
    """差し替えのクライアントで、閉じたチャンネルの失敗を 3 か所で起こす"""

    def _manager(self, closed):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        m.current_path = "/flash"
        m.sftp_client = mock.Mock()
        m.sftp_client.get_channel.return_value.closed = closed
        self._watch(m)
        return m

    def _list(self, closed, stat_error):
        m = self._manager(closed)
        m.sftp_client.listdir_attr.return_value = [
            _Attr("conf", stat_mod.S_IFDIR | 0o755),
            _Attr("link-to-dir", stat_mod.S_IFLNK | 0o777),
        ]
        m.sftp_client.stat.side_effect = stat_error
        m.list_directory("/flash")
        self._pump(lambda: self.lists or self.errors)
        # 畳むのは通知のあと（同じスレッド）。遅れて届く分まで待つ
        self._pump(lambda: False, 0.3)
        return m

    def _inspect(self, closed, stat_error=None, readlink_error=None):
        m = self._manager(closed)
        m.sftp_client.stat.return_value = _Attr("real.cfg",
                                                stat_mod.S_IFREG | 0o644)
        m.sftp_client.stat.side_effect = stat_error
        m.sftp_client.readlink.return_value = "/flash/real.cfg"
        m.sftp_client.readlink.side_effect = readlink_error
        self.result = m.inspect_link_target("/flash/link.cfg")
        return m

    def test_a_closed_channel_during_the_link_lookup_folds(self):
        """(a) 一覧のリンク先 stat で閉じていたら、一覧を配らずに畳むこと。"""
        m = self._list(True, OSError(CLOSED))

        self.assertEqual(self.lists, [], "閉じたあとの一覧が取得成功として届いた")
        self._assert_folded_once(m, "ディレクトリ一覧取得エラー")

    def test_a_closed_channel_during_readlink_folds(self):
        """(b) readlink で閉じていたら、権限変更へ進まずに畳むこと。"""
        m = self._inspect(True, readlink_error=OSError(CLOSED))

        self.assertIsNone(self.result, "閉じたのにリンク先を返している")
        self._assert_folded_once(m, "リンク先の確認エラー")

    def test_a_closed_channel_during_the_target_stat_folds(self):
        """(c) 権限変更の前の stat で閉じていたら、畳むこと。"""
        m = self._inspect(True, stat_error=OSError(CLOSED))

        self.assertIsNone(self.result)
        self._assert_folded_once(m, "リンク先の確認エラー")

    def test_a_link_error_on_an_open_channel_keeps_the_listing(self):
        """対照: チャンネルが開いていれば、これまでどおり一覧に出して畳まないこと。"""
        m = self._list(False, OSError("Permission denied"))

        self.assertEqual(len(self.lists), 1, "一覧が届かない: %s" % self.errors)
        self.assertEqual(self.errors, [])
        self.assertTrue(m.is_connected, "開いているチャンネルで畳んだ")
        self.assertEqual(self.gone, [])

    def test_a_readlink_error_on_an_open_channel_keeps_the_session(self):
        """対照: 開いたチャンネルで readlink が断られても、権限だけ返すこと。"""
        m = self._inspect(False, readlink_error=OSError("Permission denied"))

        self.assertEqual(self.result, (stat_mod.S_IFREG | 0o644, None))
        self.assertEqual(self.errors, [])
        self.assertTrue(m.is_connected)
        self.assertEqual(self.gone, [])

    def test_a_stat_error_on_an_open_channel_is_still_refused(self):
        """対照: 開いたチャンネルで読めないリンクは、理由を伝えて断るだけにすること。"""
        m = self._inspect(False, stat_error=OSError("Permission denied"))

        self.assertIsNone(self.result)
        self.assertEqual(self.errors, [
            "'link.cfg' のリンク先を読めないため、パーミッションを変更できません"
            "（Permission denied）"])
        self.assertTrue(m.is_connected)
        self.assertEqual(self.gone, [])


class ClosedChannelLinkChecksRealTest(_Base):
    """実 paramiko: 機器が SFTP のチャンネルだけを閉じる"""

    def setUp(self):
        super().setUp()
        fs = base._MemoryFS()
        fs.files[base.HOME + "/boot.bin"] = base._Node(0o644, b"x" * 10)
        self.server = _Server(fs)
        self.addCleanup(self.server.close)
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect("127.0.0.1", port=self.server.port,
                         username=base.USER, password=base.PASSWORD,
                         look_for_keys=False, allow_agent=False, timeout=10)
        self.addCleanup(self.ssh.close)
        from core.sftp_manager import SFTPManager

        self.m = SFTPManager()
        self.assertTrue(self.m.connect(self.ssh), "前提: SFTP に接続できない")
        self.addCleanup(self.m.disconnect)
        self._watch(self.m)

    def _close_channel_on_the_device(self):
        channel = self.m.sftp_client.get_channel()
        _CapturingSFTPServer.channels[-1].close()
        self.assertTrue(self._pump(lambda: channel.closed),
                        "前提: 閉じた通知が届かない")

    def _close_before_first(self, name):
        """クライアントの name を最初に呼ぶ直前に、機器側でチャンネルを閉じる"""
        client = self.m.sftp_client
        original = getattr(client, name)
        calls = []

        def wrapper(*args, **kwargs):
            if not calls:
                calls.append(args)
                self._close_channel_on_the_device()
            return original(*args, **kwargs)
        setattr(client, name, wrapper)

    def test_the_device_closing_before_the_link_lookup_folds(self):
        """(a) listdir_attr のあと、リンク先の stat の前に閉じた場合。"""
        self._close_before_first("stat")

        self.m.list_directory()
        self._pump(lambda: self.lists or self.errors)
        self._pump(lambda: False, 0.3)

        self.assertEqual(self.lists, [], "閉じたあとの一覧が取得成功として届いた")
        self._assert_folded_once(self.m, "ディレクトリ一覧取得エラー")
        self.assertTrue(self.ssh.get_transport().is_active(),
                        "SSH のセッションまで閉じた（端末側のもの）")

    def test_the_device_closing_before_readlink_folds(self):
        """(b) リンク先の stat のあと、readlink の前に閉じた場合。"""
        self._close_before_first("readlink")

        result = self.m.inspect_link_target(LINK)

        self.assertIsNone(result, "閉じたのにリンク先を返している")
        self._assert_folded_once(self.m, "リンク先の確認エラー")

    def test_choosing_chmod_on_a_link_after_the_device_closed_folds(self):
        """(c) 待機中に閉じられ、リンクの権限変更を選んだ場合（パネル経由）。

        ダイアログへ進まず、「SFTP は切断されました」を出し、一覧は残し、
        次の削除は確認を開かずに断ること。
        """
        from ui import sftp_panel as mod
        from ui.sftp_panel import SFTPPanel

        panel = SFTPPanel()
        type(self)._panels.append(panel)      # 親より先に捨てない
        self.addCleanup(panel.clear)
        panel.set_sftp_manager(self.m, "router-A", "192.0.2.10")
        self.assertTrue(self._pump(lambda: panel.model.rowCount() == 3),
                        "前提: 一覧が 3 行出ていること")
        self._close_channel_on_the_device()

        with mock.patch.object(mod.QInputDialog, "getText",
                               return_value=("", False)) as get_text:
            panel._on_chmod_selected({"name": "link-to-dir", "is_dir": True,
                                      "is_link": True,
                                      "mode": stat_mod.S_IFLNK | 0o777})
        self._pump(lambda: False, 0.3)

        get_text.assert_not_called()
        self._assert_folded_once(self.m, "リンク先の確認エラー")
        self.assertEqual(self.warning.call_count, 1, "警告が重なっている")
        self.assertEqual(panel.status_label.text(), mod.SFTPPanel.DROPPED_TEXT)
        self.assertEqual(panel.model.rowCount(), 3, "一覧まで消えた")
        with mock.patch.object(mod.QMessageBox, "question") as question:
            panel._on_delete_selected({"name": "boot.bin", "is_dir": False})
        question.assert_not_called()
        self.assertTrue(self.ssh.get_transport().is_active(),
                        "SSH のセッションまで閉じた（端末側のもの）")


if __name__ == "__main__":
    unittest.main()
