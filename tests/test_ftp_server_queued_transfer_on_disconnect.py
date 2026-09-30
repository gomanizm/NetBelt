"""データ接続を待っているアップロードを残したまま制御接続が閉じたら、その行を中断で閉じること。

何が起きていたか（実測、b2858c4。441ea02 でも同じ。127.0.0.1 のみ）:
PASV → STOR hold.cfg（150）の後、データ接続を張らずに ftplib の close()（QUIT
でも同じ。221 の後に閉じる）をすると、予約（_uploads）は外れるが、通知は
[('started', 'hold.cfg', 'upload')] のまま中断が出ず、台帳（_tx）に hold.cfg の
行が残った（パネルの行も「転送中」のまま）。同じ IP から上げ直すと、残った行に
束ねられて開始が出ず、完了だけが届いた。APPE hold.cfg（150）でも同じだった。
pyftpdlib 2.2.0 の FTPHandler.close() は、待ち行列（_in_dtp_queue）のファイルを
閉じるだけで on_incomplete_file_received を呼ばない。_Handler.close() の上書きも
予約を外すだけだった。

どう直したか: _Handler.close() で、pyftpdlib が待ち行列を消す前にそれを
控え、閉じ終えたら REIN / ABOR と同じ _abandon_queued で片付ける（予約を外し、
待っていた STOR / APPE の行を中断で閉じる）。STOR が 150 を返す時点で保存先を
'wb' で開いて 0 バイトにするのは pyftpdlib の挙動なので変えていない。

待っていた RETR（150 の後に切断）も行は開始のまま残るが、これは変えていない。
機器のプローブ（接続→RETR→即切断）を 1 行に束ねる設計で、切断した接続の
行に次の接続の RETR が束ねられることを
tests/test_ftp_server_abandoned_retr_shared_row.py の
test_a_connection_that_left_does_not_keep_the_row_open が確かめている（下の対照）。
"""
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402


class QueuedTransferOnDisconnectTest(_FtpServerCase):
    def setUp(self):
        super().setUp()
        with open(self.real("get.cfg"), "wb") as seed:
            seed.write(b"0123456789")
        self.events = []
        for signal, kind in ((self.m.transfer_started, "started"),
                             (self.m.transfer_complete, "complete"),
                             (self.m.transfer_interrupted, "interrupted")):
            slot = (lambda *args, kind=kind: self.events.append(
                (kind, args[1], args[-1])))
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)

    def _pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def _wait_event(self, event, seconds=5.0):
        deadline = time.monotonic() + seconds
        while event not in self.events and time.monotonic() < deadline:
            self._pump(0.05)
        return event in self.events

    def _queue(self, ftp, command):
        """データ接続を張らずに転送コマンドだけを受けさせる"""
        ftp.sendcmd("PASV")
        resp = ftp.sendcmd(command)
        self.assertTrue(resp.startswith("150"), resp)
        self._pump(0.3)

    def _server_connections(self):
        """待ち受けとデータ接続を除く、サーバー側の制御接続の数"""
        from pyftpdlib.handlers import FTPHandler
        try:
            handlers = list(self.m._server.ioloop.socket_map.values())
        except RuntimeError:     # 待受スレッドが書き換えている最中
            return -1
        return sum(isinstance(h, FTPHandler) for h in handlers)

    def _wait_closed_on_server(self, seconds=5.0):
        deadline = time.monotonic() + seconds
        while self._server_connections() != 0:
            self.assertLess(time.monotonic(), deadline,
                            "前提: サーバーが制御接続を閉じていない")
            self._pump(0.05)
        self._pump(0.3)

    def _check_closed_row(self, name):
        event = ("interrupted", name, "upload")
        self.assertTrue(self._wait_event(event),
                        "待っていたアップロードの行が中断で閉じられていない: %r"
                        % self.events)
        self.assertEqual(self.events, [("started", name, "upload"), event])
        self.assertEqual(self.m._tx, {}, "開始のまま残った行がある")
        self.assertEqual(self.m._uploads, {}, "予約が残った")

    def test_a_queued_stor_is_interrupted_when_the_client_drops(self):
        a = self.client()
        self._queue(a, "STOR hold.cfg")
        a.close()
        self._check_closed_row("hold.cfg")

    def test_a_queued_stor_is_interrupted_when_the_client_quits(self):
        a = self.client()
        self._queue(a, "STOR hold.cfg")
        self.assertTrue(a.quit().startswith("221"))
        self._check_closed_row("hold.cfg")

    def test_a_queued_appe_is_interrupted_when_the_client_drops(self):
        a = self.client()
        self._queue(a, "APPE hold.cfg")
        a.close()
        self._check_closed_row("hold.cfg")

    def test_the_retry_after_a_drop_gets_its_own_row(self):
        """上げ直しが残った行に束ねられず、開始から出ること"""
        a = self.client()
        self._queue(a, "STOR hold.cfg")
        a.close()
        self.assertTrue(self._wait_event(("interrupted", "hold.cfg", "upload")),
                        "待っていたアップロードの行が中断で閉じられていない: %r"
                        % self.events)
        b = self.client()
        self.assertTrue(self.upload(b, "hold.cfg", b"NEW").startswith("226"))
        self.assertTrue(self._wait_event(("complete", "hold.cfg", "upload")))
        self.assertEqual(self.events, [
            ("started", "hold.cfg", "upload"), ("interrupted", "hold.cfg", "upload"),
            ("started", "hold.cfg", "upload"), ("complete", "hold.cfg", "upload")])
        self.assertEqual(self.read("hold.cfg"), b"NEW")

    def test_a_queued_retr_row_is_kept_for_the_probe_bundling(self):
        """対照: 待っていた RETR の行は切断しても残す（プローブを 1 行に束ねる設計は変えない）"""
        a = self.client()
        self._queue(a, "RETR get.cfg")
        a.close()
        self._wait_closed_on_server()
        self.assertEqual(self.events, [("started", "get.cfg", "download")])


if __name__ == "__main__":
    unittest.main()
