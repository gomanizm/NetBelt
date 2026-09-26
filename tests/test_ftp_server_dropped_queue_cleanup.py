"""REIN / USER / ABOR で捨てた・取り消した待ち行列の転送を、予約と行を残さずに片付けること。

何が起きていたか（実測、792692f。127.0.0.1 のみ）:

  1) A が PASV → STOR hold.cfg（150）→ ABOR（225 ABOR command successful;
     data channel closed.）の後、B の STOR hold.cfg は直後も 1 秒後も 450 で、
     A が切断して初めて 226 になった。hold.cfg の行も開始のまま閉じなかった。
     pyftpdlib 2.2.0 の ftp_ABOR は受け口を閉じるだけで、データ接続を待って
     いる STOR（_in_dtp_queue）を残す。残った STOR は、後で張られたデータ接続
     からそのまま書き込まれる。
  2) 同じ接続で STOR hold.cfg と STOR other.cfg を続けて待ちにしてから REIN
     すると、other.cfg は外れて中断が出るが、hold.cfg は 450 が続き、行も
     開始のままだった。pyftpdlib は後の STOR で待ち行列を上書きするので、
     flush_account の時点で手元に残っているのは最後の 1 件だけ。
  3) PASV → RETR get.cfg（150）→ REIN → 入り直して LIST すると、get.cfg の
     行が開始のまま閉じず、LIST の進捗が ('get.cfg', 'download') として出た。
     flush_account はデータ接続を待っている RETR（_out_dtp_queue）も黙って
     捨てる。ABOR の後の LIST でも同じだった。

どう直したか: REIN / 認証済みの USER（flush_account）と ABOR の後に、捨てた
待ち行列のファイルを閉じ、この接続の予約を、続いている受信（RFC 959 どおり
REIN の後も最後まで続く）の保存先以外すべて外し、捨てた転送の行を中断で
閉じる。ABOR では待ち行列そのものも空にする（RFC 959 では ABOR が直前の
転送コマンドを取り消す）。
"""
import ftplib
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import (  # noqa: E402
    PASSWORD, USER, _FtpServerCase)


class DroppedQueueCleanupTest(_FtpServerCase):
    def setUp(self):
        super().setUp()
        self.events = []
        for signal, kind in ((self.m.transfer_started, "started"),
                             (self.m.transfer_progress, "progress"),
                             (self.m.transfer_complete, "complete"),
                             (self.m.transfer_interrupted, "interrupted")):
            slot = (lambda *args, kind=kind: self.events.append(
                (kind, args[1], args[-1])))
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)

    def _queue(self, ftp, command):
        """データ接続を張らずに転送コマンドだけを受けさせる"""
        ftp.sendcmd("PASV")
        resp = ftp.sendcmd(command)
        self.assertTrue(resp.startswith("150"), resp)

    def _abort(self, ftp):
        resp = ftp.sendcmd("ABOR")
        self.assertTrue(resp.startswith("225"), resp)

    def _relogin(self, ftp):
        self.assertTrue(ftp.sendcmd("REIN").startswith("230"))
        ftp.login(USER, PASSWORD)
        ftp.voidcmd("TYPE I")

    def _pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def _named(self, kind):
        return [(name, direction) for k, name, direction in self.events if k == kind]

    def test_abor_with_a_queued_stor_releases_the_reservation(self):
        a = self.client()
        self._queue(a, "STOR hold.cfg")
        self._abort(a)
        b = self.client()
        # 待たずに通ること（外れるのを待つ upload_eventually は使わない）
        self.assertTrue(self.upload(b, "hold.cfg", b"BBBB").startswith("226"))
        self.assertEqual(self.read("hold.cfg"), b"BBBB")
        self._pump(0.3)
        self.assertIn(("hold.cfg", "upload"), self._named("interrupted"),
                      "ABOR した STOR の行が閉じられていない: %r" % self.events)

    def test_abor_does_not_leave_the_stor_queued(self):
        """取り消した STOR へ、後で張ったデータ接続から書き込ませない"""
        a = self.client()
        self._queue(a, "STOR hold.cfg")
        self._abort(a)
        b = self.client()
        self.assertTrue(self.upload(b, "hold.cfg", b"BBBB").startswith("226"))
        host, port = a.makepasv()
        try:
            late = socket.create_connection((host, port), timeout=5)
            self.addCleanup(self._close_socket, late)
            late.sendall(b"XXXX")
            late.close()
        except OSError:
            pass    # 待っている転送が無ければ、サーバーが先に閉じることがある
        self._pump(0.5)
        self.assertEqual(self.read("hold.cfg"), b"BBBB",
                         "ABOR で取り消した STOR が後のデータ接続を受け取った")
        self.assertTrue(a.sendcmd("NOOP").startswith("200"))

    def test_rein_after_two_queued_stors_releases_both(self):
        a = self.client()
        self._queue(a, "STOR hold.cfg")
        resp = a.sendcmd("STOR other.cfg")
        self.assertTrue(resp.startswith("150"), resp)
        self._relogin(a)
        b = self.client()
        self.assertTrue(self.upload(b, "hold.cfg", b"BBBB").startswith("226"),
                        "先に待ちにした STOR の予約が残っている")
        self.assertTrue(self.upload(b, "other.cfg", b"bbbb").startswith("226"))
        self._pump(0.3)
        self.assertEqual(sorted(self._named("interrupted")),
                         [("hold.cfg", "upload"), ("other.cfg", "upload")],
                         "捨てた STOR の行が閉じられていない: %r" % self.events)
        self.assertEqual(self.m._tx, {}, "開始のまま残った行がある")

    def test_rein_with_a_queued_retr_closes_the_download_row(self):
        with open(self.real("get.cfg"), "wb") as seed:
            seed.write(b"0123456789")
        a = self.client()
        self._queue(a, "RETR get.cfg")
        self._relogin(a)
        a.retrlines("LIST", lambda line: None)
        self._pump(0.5)
        self.assertEqual(self._named("interrupted"), [("get.cfg", "download")],
                         "捨てた RETR の行が閉じられていない: %r" % self.events)
        self.assertNotIn(("get.cfg", "download"), self._named("progress"),
                         "入り直した後の LIST が get.cfg の名前で出ている")
        self.assertEqual(self.m._tx, {})

    def test_abor_with_a_queued_retr_closes_the_download_row(self):
        with open(self.real("get.cfg"), "wb") as seed:
            seed.write(b"0123456789")
        a = self.client()
        self._queue(a, "RETR get.cfg")
        self._abort(a)
        a.retrlines("LIST", lambda line: None)
        self._pump(0.5)
        self.assertEqual(self._named("interrupted"), [("get.cfg", "download")],
                         "ABOR した RETR の行が閉じられていない: %r" % self.events)
        self.assertNotIn(("get.cfg", "download"), self._named("progress"),
                         "ABOR の後の LIST が get.cfg の名前で出ている")
        self.assertEqual(self.m._tx, {})

    def test_abor_without_a_queued_transfer_is_unchanged(self):
        """待っている転送が無い ABOR は、これまでどおり 225 を返すだけ"""
        a = self.client()
        self.assertTrue(a.sendcmd("ABOR").startswith("225"))
        self.assertTrue(self.upload(a, "plain.cfg", b"PP").startswith("226"))
        self.assertEqual(self.read("plain.cfg"), b"PP")


if __name__ == "__main__":
    unittest.main()
