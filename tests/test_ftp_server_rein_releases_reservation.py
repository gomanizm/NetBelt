"""STOR を受けたまま REIN / USER で入り直したとき、捨てられた STOR の予約が残らないこと。

何が起きていたか（実測、基準 441ea02。127.0.0.1 のみ）:

  1) A が PASV → STOR hold.cfg（150。データ接続はまだ張らない）
  2) A が REIN（230）→ 同じ資格情報で入り直す
  3) B が hold.cfg へ STOR する

と、3) は直後も 1 秒後も 450 File busy で断られた。A が別名の other.cfg を
226 まで上げると外れ、B は 226 になった。USER / PASS で入り直しても同じ。
pyftpdlib 2.2.0 の FTPHandler.flush_account（REIN と認証済みの USER が呼ぶ）は、
データ接続を待っている STOR（_in_dtp_queue）を None にするだけで、ファイルを
閉じず on_incomplete_file_received も呼ばない。NetBelt の予約を外すのは受信の
完了・未完了のコールバックと close() なので、制御接続を閉じる（無通信の期限は
300 秒）か、その接続の次のアップロードが終わるまで、同じ保存先へ誰も書けなかった。
同じ理由で、パネルへ出した hold.cfg の行も開始のまま閉じられず、入り直した後の
LIST の進捗が hold.cfg の名前（アップロード）で出ていた（実測）。

どう直したか: _Handler.flush_account で、super() の前に待ち行列を控え、STOR /
APPE が捨てられたらそのファイルを閉じ、その保存先の予約だけを外し、未完了として
通知して転送の名前を降ろす。進行中の転送（RFC 959 どおり REIN の後も最後まで
続く）の予約には触れない。
"""
import ftplib
import os
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import (  # noqa: E402
    PASSWORD, USER, _FtpServerCase)


class ReinReleasesQueuedReservationTest(_FtpServerCase):
    def _queue_stor(self, ftp, name, verb="STOR"):
        """データ接続を張らずに STOR（APPE）だけを受けさせる"""
        ftp.sendcmd("PASV")
        resp = ftp.sendcmd("%s %s" % (verb, name))
        self.assertTrue(resp.startswith("150"), resp)

    def _relogin(self, ftp, how):
        if how == "REIN":
            self.assertTrue(ftp.sendcmd("REIN").startswith("230"))
            ftp.login(USER, PASSWORD)
        else:
            self.assertTrue(ftp.sendcmd("USER " + USER).startswith("331"))
            self.assertTrue(ftp.sendcmd("PASS " + PASSWORD).startswith("230"))
        ftp.voidcmd("TYPE I")

    def test_rein_with_a_queued_stor_releases_the_reservation(self):
        a = self.client()
        self._queue_stor(a, "hold.cfg")
        self._relogin(a, "REIN")
        b = self.client()
        # 待たずに通ること（外れるのを待つ upload_eventually は使わない）
        self.assertTrue(self.upload(b, "hold.cfg", b"BBBB").startswith("226"))
        self.assertEqual(self.read("hold.cfg"), b"BBBB")

    def test_user_relogin_with_a_queued_stor_releases_the_reservation(self):
        for verb in ("STOR", "APPE"):
            with self.subTest(verb=verb):
                name = "hold-%s.cfg" % verb.lower()
                a = self.client()
                self._queue_stor(a, name, verb)
                self._relogin(a, "USER")
                b = self.client()
                self.assertTrue(self.upload(b, name, b"BBBB").startswith("226"))
                self.assertEqual(self.read(name), b"BBBB")

    def test_the_queued_upload_row_is_closed_as_interrupted(self):
        """捨てた STOR の行を開始のまま残さず、後の LIST をその名前で出さない"""
        events = []
        for signal, kind in ((self.m.transfer_interrupted, "interrupted"),
                             (self.m.transfer_progress, "progress")):
            slot = (lambda *args, kind=kind: events.append((kind,) + args))
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)
        a = self.client()
        self._queue_stor(a, "hold.cfg")
        self._relogin(a, "REIN")
        a.retrlines("LIST", lambda line: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        interrupted = [e for e in events if e[0] == "interrupted"]
        self.assertEqual([(e[2], e[3]) for e in interrupted], [("hold.cfg", "upload")],
                         "捨てた STOR の行が閉じられていない: %r" % events)
        stale = [e for e in events if e[0] == "progress" and e[2] == "hold.cfg"]
        self.assertEqual(stale, [], "入り直した後の LIST が hold.cfg の名前で出ている")

    def test_rein_keeps_the_reservation_of_an_upload_in_progress(self):
        """進行中の転送は REIN の後も続くので、その予約は外さない"""
        a = self.client()
        data = self.start_upload(a, "busy.cfg", b"AAAA")
        time.sleep(0.2)     # データ接続が受信を始めるまで
        self.assertTrue(a.sendcmd("REIN").startswith("230"))
        b = self.client()
        with self.assertRaises(ftplib.error_temp) as refused:
            b.transfercmd("STOR busy.cfg")
        self.assertIn("450", str(refused.exception))
        self.assertTrue(self.finish_upload(a, data, b"aaaa").startswith("226"))
        self.assertEqual(self.read("busy.cfg"), b"AAAAaaaa")
        # 転送が終われば外れる
        self.assertTrue(self.upload_eventually(b, "busy.cfg", b"BB").startswith("226"))
        self.assertEqual(self.read("busy.cfg"), b"BB")


if __name__ == "__main__":
    unittest.main()
