"""確認を経ていないアップロードの途中で SFTP のチャンネルが閉じられたときの文面を検証する。

実測（基準 441ea02）:
  overwrite=False のアップロードで、put のあと・最終名への置き換えの前に
  機器が SFTP のチャンネルだけを閉じると（SSH のトランスポートは生きたまま）、
  送ったあとの再確認の STAT は OSError('Socket is closed') で落ちる。
  _remote_probe（src/core/sftp_manager.py:483-484）はこれも「無い」と読むので
  改名へ進み、rename は送る前に同じ OSError で落ちる。845-863 の
  `if not overwrite:` の枝は理由を見ずに、
    『リモートの 'running.cfg' へ置き換えられませんでした（既にある可能性が
     あります）。…上書きしてよければ一覧を更新してからやり直してください:
     Socket is closed。SFTP接続を切断しました。接続し直してください』
  と、推測の文面と切断の文面を 1 つの通知に並べた。実際には最終名は無く
  （None）、中身は一時名に残っていた（b'hostname R1'）。localhost の実
  paramiko サーバで 3/3 回。既にあるわけではないのに「上書きしてよければ
  やり直して」と案内し、利用者を上書きの判断へ誘う。

直し方:
  置き換えの失敗を受ける except の中で、確認を経ていない送信なら先に
  _channel_closed() を見る。閉じていれば一時名を残す印を立て、
  『リモートの '名前' へ置き換える前に SFTP のチャンネルが閉じられました。
  最終名には触れていません。転送した内容は機器の一時名 … に残っています:
  <理由>』の _DroppedConnection を上げる。閉じたチャンネルの OSError は
  送る前に上がるので、改名の要求は届いていない。_fail は切断として 1 回だけ
  畳む。開いたチャンネルで断られたとき（既にある）の文面と挙動は変えない。
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import paramiko
from paramiko import SFTPServer
from paramiko.sftp import CMD_STAT

import test_sftp_upload_tmp_mode as base   # _MemoryFS / _SftpInterface / _Auth

sys.path.insert(0, "src")

DROPPED_SUFFIX = "。SFTP接続を切断しました。接続し直してください"
CONTENT = b"hostname R1"


class _CapturingSFTPServer(SFTPServer):
    """機器側から閉じられるよう、SFTP のチャンネルを控えておく"""

    channels = []

    def __init__(self, channel, name, server, sftp_si, *args, **kwargs):
        super().__init__(channel, name, server, sftp_si, *args, **kwargs)
        type(self).channels.append(channel)


class _CloseAfterPutInterface(base._SftpInterface):
    """put の確認（一時名への STAT）に答えたあと、少し置いてチャンネルを閉じる機器"""

    def stat(self, path):
        result = super().stat(path)
        if ".netbelt-part" in path:
            threading.Timer(0.05, _CapturingSFTPServer.channels[-1].close).start()
        return result


class _Server:
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
                                    _CloseAfterPutInterface, self.fs)
            t.start_server(server=base._Auth())
            self.transports.append(t)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for t in list(self.transports):
            t.close()


class UnconfirmedUploadClosedTextTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-closed-text-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with open(self.local, "wb") as f:
            f.write(CONTENT)

    def _pump(self, check, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _watch(self, m):
        errors, dropped = [], []
        m.error_occurred.connect(errors.append)
        m.disconnected.connect(lambda: dropped.append(True))
        type(self)._keep.append(m)
        return errors, dropped

    def _mock_manager(self):
        """put のあとで機器が SFTP のチャンネルを閉じる、差し替えのマネージャ"""
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        m.is_connected = True
        m.current_path = "/flash"
        c = m.sftp_client = mock.Mock()
        channel = c.get_channel.return_value
        channel.closed = False
        state = {"closed": False}

        def put(*args, **kwargs):
            channel.closed = True     # 転送のあと、機器が SFTP のチャンネルを閉じた
            state["closed"] = True

        def stat(*args, **kwargs):
            if state["closed"]:
                raise OSError("Socket is closed")
            raise IOError("No such file")
        c.put.side_effect = put
        c.stat.side_effect = stat
        c.rename.side_effect = OSError("Socket is closed")
        c.posix_rename.side_effect = OSError("Socket is closed")
        c.remove.side_effect = OSError("Socket is closed")
        m.list_directory = mock.Mock()
        return m, c

    def _assert_accurate(self, errors, dropped, m):
        self.assertEqual(len(errors), 1, errors)
        text = errors[0]
        self.assertNotIn("既にある可能性", text, "閉じたチャンネルを「既にある」と推測した")
        self.assertNotIn("上書きしてよければ", text, "上書きの判断へ誘う案内を出した")
        self.assertIn("チャンネルが閉じられました", text)
        self.assertIn("最終名には触れていません", text)
        self.assertIn(".running.cfg.netbelt-part.", text, "一時名の在処を伝えていない")
        self.assertTrue(text.endswith("Socket is closed" + DROPPED_SUFFIX), text)
        self.assertFalse(m.is_connected, "閉じたチャンネルを接続中のまま残した")
        self.assertEqual(dropped, [True], "切断の通知は 1 回")

    def test_a_channel_closed_before_the_rename_is_reported_as_closed(self):
        """差し替え: 置き換える前に閉じたなら、「既にある」ではなく閉じたと伝えること。"""
        m, c = self._mock_manager()
        errors, dropped = self._watch(m)
        m.upload_file(self.local, "/flash/running.cfg", overwrite=False)
        self.assertTrue(self._pump(lambda: errors and dropped), "前提: 失敗が届かない")
        self._pump(lambda: False, 0.3)
        self._assert_accurate(errors, dropped, m)
        c.remove.assert_not_called()   # 一時名は消さない（唯一の写し）

    def test_a_refusal_on_an_open_channel_keeps_the_existing_text(self):
        """対照: 開いたチャンネルで断られたら、これまでどおり「既にある可能性」と伝え、畳まないこと。"""
        m, c = self._mock_manager()
        channel = c.get_channel.return_value
        c.put.side_effect = None
        c.stat.side_effect = IOError("No such file")
        c.rename.side_effect = IOError("Failure")
        errors, dropped = self._watch(m)
        m.upload_file(self.local, "/flash/running.cfg", overwrite=False)
        self.assertTrue(self._pump(lambda: errors), "前提: 失敗が届かない")
        self._pump(lambda: False, 0.3)
        self.assertFalse(channel.closed)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("既にある可能性", errors[0])
        self.assertTrue(m.is_connected)
        self.assertEqual(dropped, [])

    def test_a_real_device_closing_the_channel_after_put(self):
        """実 paramiko: 最終名は作られず、中身は一時名に残り、文面も正確であること。"""
        from core.sftp_manager import SFTPManager
        _CapturingSFTPServer.channels.clear()
        fs = base._MemoryFS()
        server = _Server(fs)
        self.addCleanup(server.close)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", port=server.port, username=base.USER,
                    password=base.PASSWORD, look_for_keys=False,
                    allow_agent=False, timeout=10)
        self.addCleanup(ssh.close)
        m = SFTPManager()
        errors, dropped = self._watch(m)
        self.assertTrue(m.connect(ssh), errors)
        m.list_directory = mock.Mock()
        final = base.HOME + "/running.cfg"

        real_request = paramiko.SFTPClient._request
        seen = {"final_stats": 0, "closed_before_recheck": None}

        def gated_request(client, t, *args):
            # 送ったあとの再確認（最終名への 2 回目の STAT）の前に、閉じた通知が
            # 届くのを待つ（機器が閉じたのを先に受け取っている状況）
            name = args[0] if args else ""
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            name = str(name)
            if (t == CMD_STAT and name.endswith("/running.cfg")
                    and ".netbelt-part" not in name):
                seen["final_stats"] += 1
                if seen["final_stats"] == 2:
                    deadline = time.time() + 5
                    while not client.sock.closed and time.time() < deadline:
                        time.sleep(0.01)
                    seen["closed_before_recheck"] = client.sock.closed
            return real_request(client, t, *args)

        with mock.patch.object(paramiko.SFTPClient, "_request", gated_request):
            m.upload_file(self.local, final, overwrite=False)
            self.assertTrue(self._pump(lambda: errors and dropped, 8.0),
                            "前提: 失敗が届かない %r" % errors)
        self._pump(lambda: False, 0.3)
        self.assertTrue(seen["closed_before_recheck"], "前提: 再確認の前に閉じていない")
        with fs.lock:
            self.assertNotIn(final, fs.files, "最終名が作られた")
            parts = [bytes(fs.files[p].data) for p in fs.parts()]
        self.assertEqual(parts, [CONTENT], "転送した内容が一時名に残っていない")
        self._assert_accurate(errors, dropped, m)
        self.assertTrue(ssh.get_transport().is_active(), "SSH まで切った")


if __name__ == "__main__":
    unittest.main()
