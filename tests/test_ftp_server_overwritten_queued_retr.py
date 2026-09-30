"""待ち行列の RETR を後の転送コマンドで上書きしたとき、先の RETR の行を残さないこと。

何が起きていたか（実測、c2bb66a。127.0.0.1 のみ）: pyftpdlib 2.2.0 は、データ
接続を待っている RETR の後に来た転送コマンド（RETR・LIST など）で待ち行列
（_out_dtp_queue）を上書きし、先の RETR を黙って捨てる。こちらの片付け
（REIN / USER / ABOR の _abandon_queued）が見るのは最後の 1 件だけで、接続が
覚える行の番号（_tx_row_id）も RETR のたびに上書きされていた。そのため、
データ接続を張らずに

  1) PASV → RETR a.cfg → RETR b.cfg → ABOR（225）: 通知は [開始 a, 開始 b, 中断 b]
     で、a.cfg の行が台帳（_tx）とパネルに開始のまま残った。REIN（230）でも同じ
  2) その後に同じ接続で a.cfg を取り直すと、残った行に束ねられて開始の通知が
     出ず、完了だけが届いた
  3) RETR a.cfg → RETR b.cfg の後にデータ接続を張って b.cfg を取り切っても、
     a.cfg の行は開始のまま残った
  4) RETR a.cfg の後にデータ接続なしの LIST（150）→ ABOR でも、a.cfg の行が残った

どう直したか: 待ち行列を積む push_dtp_data で、上書きされた RETR をその場で
片付ける（ファイルを閉じ、その行を離れる。行はほかの接続が加わっていなければ
中断で閉じる。_may_close_row と同じ判断）。同じファイルの RETR を積み直した
ときは、今までどおり同じ行に束ねたままにする。REIN / USER / ABOR の片付けも
同じ関数を使う。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402

A = b"A-CONTENT-" * 8
B = b"B-CONTENT-" * 8


class OverwrittenQueuedRetrTest(_FtpServerCase):
    def setUp(self):
        super().setUp()
        for name, payload in (("a.cfg", A), ("b.cfg", B)):
            with open(self.real(name), "wb") as seed:
                seed.write(payload)
        self.events = []
        for signal, kind in ((self.m.transfer_started, "started"),
                             (self.m.transfer_complete, "complete"),
                             (self.m.transfer_interrupted, "interrupted")):
            slot = (lambda *args, kind=kind: self.events.append((kind, args[1])))
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)

    def _pump(self, seconds=0.3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def _sync(self, ftp):
        """直前のコマンドの処理を、応答の後の台帳の更新まで待つ"""
        self.assertTrue(ftp.sendcmd("NOOP").startswith("200"))
        self._pump()

    def _queue(self, ftp, *commands):
        """データ接続を張らずに転送コマンドだけを受けさせる"""
        ftp.sendcmd("PASV")
        for command in commands:
            resp = ftp.sendcmd(command)
            self.assertTrue(resp.startswith("150"), resp)
        self._sync(ftp)

    def _names(self):
        return sorted(os.path.basename(path) for (_ip, path, _d) in self.m._tx)

    def _receive_queued(self, ftp):
        """PASV を張り直し、待ち行列の転送をデータ接続で受け取る"""
        host, port = ftp.makepasv()
        data = socket.create_connection((host, port), timeout=10)
        self.addCleanup(self._close_socket, data)
        got = b""
        while True:
            chunk = data.recv(65536)
            if not chunk:
                break
            got += chunk
        data.close()
        self.assertTrue(ftp.voidresp().startswith("226"))
        self._sync(ftp)
        return got

    def _check_drop_after_two_retrs(self, how):
        ftp = self.client()
        self._queue(ftp, "RETR a.cfg", "RETR b.cfg")
        resp = ftp.sendcmd(how)
        self.assertTrue(resp.startswith("225" if how == "ABOR" else "230"), resp)
        self._sync(ftp)
        self.assertEqual(self._names(), [],
                         "%s の後に行が開始のまま残った: %r" % (how, self.events))
        self.assertEqual(sorted(e for e in self.events if e[0] == "interrupted"),
                         [("interrupted", "a.cfg"), ("interrupted", "b.cfg")],
                         "捨てた RETR の行が中断で閉じられていない: %r" % self.events)
        return ftp

    def test_abor_after_two_queued_retrs_closes_both_rows(self):
        self._check_drop_after_two_retrs("ABOR")

    def test_rein_after_two_queued_retrs_closes_both_rows(self):
        self._check_drop_after_two_retrs("REIN")

    def test_a_retry_after_abor_is_reported_as_a_new_transfer(self):
        ftp = self._check_drop_after_two_retrs("ABOR")
        del self.events[:]
        got = []
        self.assertTrue(ftp.retrbinary("RETR a.cfg", got.append).startswith("226"))
        self._sync(ftp)
        self.assertEqual(b"".join(got), A)
        self.assertEqual(self.events, [("started", "a.cfg"), ("complete", "a.cfg")],
                         "取り直しが残った行に束ねられ、開始が通知されていない")
        self.assertEqual(self._names(), [])

    def test_transferring_the_later_retr_leaves_no_row_of_the_earlier(self):
        ftp = self.client()
        self._queue(ftp, "RETR a.cfg", "RETR b.cfg")
        self.assertEqual(self._receive_queued(ftp), B)
        self.assertEqual(self._names(), [],
                         "上書きされた RETR の行が開始のまま残った: %r" % self.events)
        # a.cfg は RETR b.cfg で待ち行列から外れた時点で中断になる
        self.assertEqual(self.events,
                         [("started", "a.cfg"), ("interrupted", "a.cfg"),
                          ("started", "b.cfg"), ("complete", "b.cfg")])

    def test_a_queued_retr_overwritten_by_list_is_closed(self):
        ftp = self.client()
        self._queue(ftp, "RETR a.cfg", "LIST")
        self.assertTrue(ftp.sendcmd("ABOR").startswith("225"))
        self._sync(ftp)
        self.assertEqual(self.events, [("started", "a.cfg"), ("interrupted", "a.cfg")])
        self.assertEqual(self._names(), [])

    def test_queueing_the_same_file_again_keeps_one_row(self):
        """対照: 同じファイルの RETR を積み直したときは、今までどおり 1 行に束ねる"""
        ftp = self.client()
        self._queue(ftp, "RETR a.cfg", "RETR a.cfg")
        self.assertEqual(self._receive_queued(ftp), A)
        self.assertEqual(self.events, [("started", "a.cfg"), ("complete", "a.cfg")])
        self.assertEqual(self._names(), [])

    def test_an_overwritten_retr_keeps_a_row_another_connection_is_on(self):
        """対照: 同じ行にほかの接続が待ち行列で加わっていれば、上書きでは閉じない
        （_may_close_row と同じ判断）。残った接続が ABOR した時点で閉じる"""
        p1 = self.client()
        self._queue(p1, "RETR a.cfg")
        p2 = self.client()
        self._queue(p2, "RETR a.cfg")            # P1 の行に束ねられる
        resp = p1.sendcmd("RETR b.cfg")          # P1 の a.cfg は上書きされる
        self.assertTrue(resp.startswith("150"), resp)
        self._sync(p1)
        self.assertEqual(self.events, [("started", "a.cfg"), ("started", "b.cfg")])
        self.assertEqual(self._names(), ["a.cfg", "b.cfg"])
        self.assertTrue(p2.sendcmd("ABOR").startswith("225"))
        self._sync(p2)
        self.assertEqual(self.events, [("started", "a.cfg"), ("started", "b.cfg"),
                                       ("interrupted", "a.cfg")])
        self.assertEqual(self._names(), ["b.cfg"])


if __name__ == "__main__":
    unittest.main()
