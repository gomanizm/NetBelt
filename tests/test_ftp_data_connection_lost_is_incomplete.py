"""データ接続が失われて終わったアップロードを「完了」と記録しないこと。

何が起きていたか（実測、792692f。127.0.0.1 のみ）: 792692f はデータ接続に
TCP keepalive を掛け、黙って消えた書き手を確かめが尽きたところで切るように
した。ところが pyftpdlib 2.2.0 の recv（ioloop.AsyncChat.recv）は、
ETIMEDOUT・ENOTCONN・ECONNABORTED などの接続断のエラーを EOF と同じく
handle_close へ回し、受信中の handle_close は転送を完了として扱う。
サーバー側データソケットの recv にエラーを差し込むと、10060 / 10057 では
A に 226 Transfer complete、transfer_complete が出て、中身は途中の b'AAAA'
だった（10052 だけ 426 と中断）。keepalive が尽きたときに Windows がどの
エラーを返すかは localhost では作れないが、どれでも途中までのファイルが
完了と記録されうる。直す前（keepalive なし）は、消えた書き手はデータ接続の
無通信期限（300 秒）で 421 と中断になっていた。
再起動した相手が keepalive の確かめに RST を返すと ECONNRESET になるが、
これも完了として扱われる。

どう直したか: _ProgressDTP.recv で、受信中に相手が消えたことを示すエラー
（ETIMEDOUT / ENOTCONN / ECONNABORTED / ENETRESET / ECONNRESET）を受けたら、
426 で未完了として閉じる（予約は on_incomplete_file_received が外し、行は
中断で閉じる）。RST（ECONNRESET）は、当初は既存の挙動として完了のまま残し、
keepalive の確かめを送り始める DATA_KEEPALIVE_SECONDS ほど黙った後のものだけを
未完了にしていた。1.3.4（R03）で、黙っていた時間によらず未完了にした。
Windows は読み終えていない受信データを RST の到着で捨てるので、データの直後の
RST でも 0 バイトや途中までのファイルになりうる（実測）。vsftpd も受信中の
読み取りエラーは 426 にしている。b'' の通常の EOF（FIN）は、これまでどおり
完了。実際のソケットでの RST は test_ftp_data_connection_reset_is_interrupted.py。
"""
import errno
import ftplib
import os
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402


class _FailingRecv:
    """recv だけ指定の errno で失敗させる（ほかは本物のソケットへ委ねる）"""
    def __init__(self, real, code):
        self._real = real
        self._code = code

    def recv(self, _size):
        raise OSError(self._code, "injected")

    def __getattr__(self, name):
        return getattr(self._real, name)


class FtpDataConnectionLostTest(_FtpServerCase):
    def setUp(self):
        super().setUp()
        self.events = []
        for signal, kind in ((self.m.transfer_complete, "complete"),
                             (self.m.transfer_interrupted, "interrupted")):
            slot = (lambda *args, kind=kind: self.events.append((kind, args[1])))
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)

    def _receiving_handler(self, received, timeout=5.0):
        """受信中で、received バイトを受け取り終えたサーバー側データ接続を返す"""
        from pyftpdlib.handlers import DTPHandler
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                handlers = list(self.m._server.ioloop.socket_map.values())
            except RuntimeError:
                handlers = []
            for handler in handlers:
                if (isinstance(handler, DTPHandler) and handler.receive
                        and handler.tot_bytes_received >= received):
                    return handler
            time.sleep(0.02)
        self.fail("受信中のデータ接続が見つからない")

    def _upload_then_fail_recv(self, name, code, pause=0.0, silent=0.0):
        """b'AAAA' を受け取らせた後、次の recv を code で失敗させて A の応答を返す。

        pause を渡したら、その秒数黙ってから b'CCCC' を送り、それも受け取らせる。
        silent を渡したら、最後のデータを受け取らせてからその秒数黙った後で
        失敗させる
        """
        a = self.client()
        data = self.start_upload(a, name, b"AAAA")
        received = 4
        if pause:
            self._receiving_handler(received)
            time.sleep(pause)
            data.sendall(b"CCCC")
            received = 8
        handler = self._receiving_handler(received)
        if silent:
            time.sleep(silent)
        handler.socket = _FailingRecv(handler.socket, code)
        try:
            data.sendall(b"BBBB")    # 読める状態にして recv を呼ばせる
        except OSError:
            pass
        try:
            return a.voidresp()
        except ftplib.Error as exc:
            return str(exc)

    def _pump(self, seconds=0.3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def _assert_interrupted(self, name, reply):
        self.assertTrue(reply.startswith("426"), "未完了の応答になっていない: %r" % reply)
        self._pump()
        self.assertIn(("interrupted", name), self.events)
        self.assertNotIn(("complete", name), self.events,
                         "途中までのアップロードが完了と記録された")
        # 予約は外れている（B の同名の STOR が待たずに通る）
        b = self.client()
        self.assertTrue(self.upload(b, name, b"Z").startswith("226"))

    def test_lost_connection_errors_end_the_upload_as_interrupted(self):
        for label in ("ETIMEDOUT", "ENOTCONN", "ECONNABORTED", "ENETRESET"):
            with self.subTest(errno=label):
                name = "lost-%s.cfg" % label.lower()
                reply = self._upload_then_fail_recv(name, getattr(errno, label))
                self._assert_interrupted(name, reply)

    def test_a_reset_right_after_data_is_interrupted(self):
        """データの直後の RST（ECONNRESET）も、未完了として扱う"""
        reply = self._upload_then_fail_recv("reset.cfg", errno.ECONNRESET)
        self._assert_interrupted("reset.cfg", reply)

    def test_a_reset_after_keepalive_silence_is_interrupted(self):
        """keepalive の確かめを送り始める時間を過ぎた接続での RST も、未完了として扱う。

        RST は、黙っていた時間によらず未完了にする。keepalive は、RST を
        返さずに黙って消えた相手を見つける別の経路で、再起動した相手が
        確かめに RST を返したときも、ほかの RST と同じく未完了になる。
        DATA_KEEPALIVE_SECONDS の差し替え（0 秒）は時間で見分けていたころの
        名残で、変わるのはデータ接続に掛ける keepalive の設定だけ。RST の
        判定には効かない
        """
        with mock.patch("core.ftp_server.DATA_KEEPALIVE_SECONDS", 0):
            reply = self._upload_then_fail_recv("probe-reset.cfg", errno.ECONNRESET)
        self._assert_interrupted("probe-reset.cfg", reply)

    def test_a_reset_slightly_short_of_the_keepalive_time_is_interrupted(self):
        """確かめを送り始める秒数にわずかに届かないうちの RST も、未完了として扱う。

        DATA_KEEPALIVE_SECONDS を 2 秒にし、最後のデータから 1.6 秒黙った後
        （keepalive の確かめを送り始める前）に RST を受ける。RST は黙っていた
        時間によらず未完了にするので、黙っていた時間も、確かめへの返事か
        どうかも見分けない（keepalive は、黙って消えた相手を見つける別の経路）。
        名前と秒数は、時間で見分けていたころ（1.3.3 まで）に、時計の粒度で
        黙っていた時間をわずかに短く測る場合を試していた名残
        """
        with mock.patch("core.ftp_server.DATA_KEEPALIVE_SECONDS", 2):
            reply = self._upload_then_fail_recv("short.cfg", errno.ECONNRESET,
                                                silent=1.6)
        self._assert_interrupted("short.cfg", reply)

    def test_a_reset_after_resumed_data_is_interrupted(self):
        """途中で黙ってから再開したデータの直後の RST も、未完了として扱う
        （黙っていた時間では見分けない）"""
        # 途中で 2.5 秒黙り、RST は再開したデータの直後。以前は最後のデータから
        # の時間で見分けていたので、これは完了になっていた
        with mock.patch("core.ftp_server.DATA_KEEPALIVE_SECONDS", 3):
            reply = self._upload_then_fail_recv("resumed.cfg", errno.ECONNRESET,
                                                pause=2.5)
        self._assert_interrupted("resumed.cfg", reply)

    def test_a_normal_end_of_data_is_still_complete(self):
        a = self.client()
        self.assertTrue(self.upload(a, "normal.cfg", b"AAAA").startswith("226"))
        self._pump()
        self.assertIn(("complete", "normal.cfg"), self.events)
        self.assertEqual(self.read("normal.cfg"), b"AAAA")

    def test_a_silent_but_alive_writer_outlives_the_keepalive_probes(self):
        """keepalive の確かめが始まる時間を超えて黙っても、生きている書き手は切らない"""
        with mock.patch("core.ftp_server.DATA_KEEPALIVE_SECONDS", 1), \
                mock.patch("core.ftp_server.DATA_KEEPALIVE_INTERVAL_SECONDS", 1):
            a = self.client()
            data = self.start_upload(a, "quiet.cfg", b"AAAA")
            sock = self._receiving_handler(4).socket
        if hasattr(socket, "TCP_KEEPIDLE"):
            # 前提: この接続は 1 秒黙ったら確かめを送る設定になっている
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE), 1)
        time.sleep(3.5)
        b = self.client()
        with self.assertRaises(ftplib.error_temp) as refused:
            b.transfercmd("STOR quiet.cfg")
        self.assertIn("450", str(refused.exception))
        self.assertTrue(self.finish_upload(a, data, b"aaaa").startswith("226"))
        self.assertEqual(self.read("quiet.cfg"), b"AAAAaaaa")


if __name__ == "__main__":
    unittest.main()
