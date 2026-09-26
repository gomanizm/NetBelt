"""待ち行列の STOR を後の STOR / STOU で上書きしたとき、先の STOR の行と予約を残さないこと。

何が起きていたか（実測、71e413a。127.0.0.1 のみ）: pyftpdlib 2.2.0 は、データ
接続を待っている STOR の後に来た STOR（APPE も同じ）・STOU で待ち行列
（_in_dtp_queue）を上書きし、先の STOR を黙って捨てる（ファイルも閉じない）。
こちらの片付け（REIN / USER / ABOR の _abandon_queued）はその時点でしか
走らないので、ABOR せずに後の方を送り切ると

  1) PASV → STOR hold.cfg → STOR other.cfg → データ接続で other.cfg を送り切る:
     通知は [開始 hold, 開始 other, 完了 other] で、hold.cfg の行が台帳（_tx）と
     パネルに開始のまま残った
  2) その後に同じ IP から hold.cfg を上げ直すと、残った行に束ねられて開始の
     通知が出ず、完了だけが届いた（別の接続からでも同じ）
  3) other.cfg を送り終えるまで hold.cfg の予約が残り、別の接続の STOR hold.cfg
     は 450 で断られた（もう誰も書かない保存先なのに）
  4) STOR hold.cfg の後の STOU でも hold.cfg の行が残り、STOU の進捗が
     hold.cfg の名前で出た

RETR の側（push_dtp_data で片付ける）は 71e413a で直っていた。

どう直したか: STOR / STOU で待ち行列が別の受信に置き換わったら、その場で先の
ファイルを閉じ、この接続の予約を新しい保存先以外すべて外し、外した保存先の
行を中断で閉じる（REIN / USER / ABOR と同じ片付け）。大文字小文字だけ違う
名前で同じ保存先を積み直したとき（予約は同じ）も、先の名前の行は閉じる。
同じパスの STOR を積み直したときは、今までどおり同じ行に束ねたままにする。
"""
import ftplib
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402

OTHER = b"OTHER-CONTENT-" * 8
HOLD = b"HOLD-CONTENT-" * 8


class OverwrittenQueuedStorTest(_FtpServerCase):
    def setUp(self):
        super().setUp()
        self.events = []
        for signal, kind in ((self.m.transfer_started, "started"),
                             (self.m.transfer_progress, "progress"),
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

    def _send_queued(self, ftp, payload):
        """PASV を張り直し、待ち行列の受信へデータ接続で payload を送り切る"""
        host, port = ftp.makepasv()
        data = socket.create_connection((host, port), timeout=10)
        self.addCleanup(self._close_socket, data)
        data.sendall(payload)
        data.close()
        self.assertTrue(ftp.voidresp().startswith("226"))
        self._sync(ftp)

    def _names(self):
        return sorted(os.path.basename(path) for (_ip, path, _d) in self.m._tx)

    def _closing(self):
        """開始・進捗以外（行を閉じる通知）"""
        return [e for e in self.events if e[0] in ("complete", "interrupted")]

    def test_uploading_the_later_stor_leaves_no_row_of_the_earlier(self):
        ftp = self.client()
        self._queue(ftp, "STOR hold.cfg", "STOR other.cfg")
        self._send_queued(ftp, OTHER)
        self.assertEqual(self.read("other.cfg"), OTHER)
        self.assertEqual(self._names(), [],
                         "上書きされた STOR の行が開始のまま残った: %r" % self.events)
        self.assertEqual(self.m._uploads, {})
        # hold.cfg は STOR other.cfg で待ち行列から外れた時点で中断になる
        self.assertEqual([e for e in self.events if e[0] != "progress"],
                         [("started", "hold.cfg"), ("interrupted", "hold.cfg"),
                          ("started", "other.cfg"), ("complete", "other.cfg")])

    def test_a_retry_after_the_overwrite_is_reported_as_a_new_transfer(self):
        a = self.client()
        self._queue(a, "STOR hold.cfg", "STOR other.cfg")
        self._send_queued(a, OTHER)
        for ftp in (self.client(), a):   # 同じ IP の別の接続と、同じ接続から
            del self.events[:]
            self.assertTrue(self.upload(ftp, "hold.cfg", HOLD).startswith("226"))
            self._sync(ftp)
            self.assertEqual(self.read("hold.cfg"), HOLD)
            self.assertEqual(self._closing(), [("complete", "hold.cfg")])
            self.assertEqual(self.events[0], ("started", "hold.cfg"),
                             "上げ直しが残った行に束ねられ、開始が通知されていない: %r"
                             % self.events)
            self.assertEqual(self._names(), [])

    def test_the_overwritten_stor_releases_its_path_at_once(self):
        a = self.client()
        self._queue(a, "STOR hold.cfg", "STOR other.cfg")
        b = self.client()
        # 待たずに通ること（外れるのを待つ upload_eventually は使わない）
        self.assertTrue(self.upload(b, "hold.cfg", HOLD).startswith("226"),
                        "上書きされた STOR の予約が残っている")
        # 待ち行列に残っている方（other.cfg）の予約は外さない（対照）
        with self.assertRaises(ftplib.error_temp):
            self.upload(b, "other.cfg", b"from-b")
        self._send_queued(a, OTHER)
        self.assertEqual(self.read("other.cfg"), OTHER)
        self.assertEqual(self.read("hold.cfg"), HOLD,
                         "捨てた STOR の接続が hold.cfg へ書き込んだ")
        self.assertEqual(self._names(), [])
        self.assertEqual(self.m._uploads, {})

    def test_a_queued_stor_overwritten_by_stou_is_closed(self):
        ftp = self.client()
        self._queue(ftp, "STOR hold.cfg", "STOU")
        self.assertEqual(self.events, [("started", "hold.cfg"),
                                       ("interrupted", "hold.cfg")])
        self.assertEqual(self.m._uploads, {})
        self._send_queued(ftp, OTHER)
        self.assertNotIn(("progress", "hold.cfg"), self.events,
                         "STOU の進捗が捨てた STOR の名前で出た")
        self.assertEqual(self._names(), [])

    def test_a_differently_cased_name_of_the_same_file_leaves_no_row(self):
        """Windows では大文字小文字だけ違う名前は同じ保存先（予約は同じ鍵）だが、
        行はパスごとに持つ。先の名前の行も閉じること（Windows 以外では別の保存先）"""
        ftp = self.client()
        self._queue(ftp, "STOR hold.cfg", "STOR HOLD.CFG")
        self._send_queued(ftp, HOLD)
        self.assertEqual(self.read("HOLD.CFG"), HOLD)
        self.assertEqual([e for e in self.events if e[0] != "progress"],
                         [("started", "hold.cfg"), ("interrupted", "hold.cfg"),
                          ("started", "HOLD.CFG"), ("complete", "HOLD.CFG")])
        self.assertEqual(self._names(), [])
        self.assertEqual(self.m._uploads, {})

    def test_queueing_the_same_path_again_keeps_one_row(self):
        """対照: 同じ保存先の STOR を積み直したときは、今までどおり 1 行に束ねる"""
        ftp = self.client()
        self._queue(ftp, "STOR hold.cfg", "STOR hold.cfg")
        self._send_queued(ftp, HOLD)
        self.assertEqual(self.read("hold.cfg"), HOLD)
        self.assertEqual([e for e in self.events if e[0] != "progress"],
                         [("started", "hold.cfg"), ("complete", "hold.cfg")])
        self.assertEqual(self._names(), [])
        self.assertEqual(self.m._uploads, {})


if __name__ == "__main__":
    unittest.main()
