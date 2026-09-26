"""待ち行列に置いた RETR を捨てた接続が、同じ IP の別の接続の行を中断にしないこと。

何が起きていたか（実測、6305095。127.0.0.1 のみ）: 131a496 で、REIN / USER /
ABOR で捨てた待ち行列の RETR の行を中断で閉じるようにした。閉じる鍵は
(IP, ファイル, 'download') だが、台帳（マネージャの _tx）の行は同じ IP の
接続で共有される（Cisco IOS が本転送の前に行うプローブを 1 行に束ねるため）。
そのため、

  1) P1 が PASV → RETR get.cfg（150、待ち行列のまま）。P3 が同じファイルを
     取得中（P1 の行に束ねられる）に P1 が ABOR / REIN すると、P3 の取得中に
     行が中断になり、P3 の 226 の後に完了の行がもう 1 行できた
     （パネルの履歴: [中断, 完了]。基準 441ea02 では [完了]）。
  2) P2 が同じファイルを取り切って行を閉じ、その後で P3 が取得を始めると、
     P1 の ABOR / REIN で P3 の新しい行が中断になった
     （[完了, 中断, 完了]。基準では [完了, 完了]）。
  3) P2 が行を閉じた後に P1 が ABOR すると、行が無いのに中断が通知され、
     パネルのログに「転送中断: get.cfg」が出た（基準では何も出ない）。

どう直したか: 行を作るたびに番号を振り、RETR を受けた接続はその番号を
覚える。捨てた RETR の行は、自分が加わった行がまだ開いていて、その行に
加わったほかの（切断していない）接続が無いときだけ閉じる（ほかの接続は、
転送中のものも待ち行列に置いているだけのものも数える）。
"""
import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402

# 取得を途中で止めておけるよう、ソケットの送受信バッファより十分大きくする
SIZE = 32 * 1024 * 1024


class AbandonedRetrSharedRowTest(_FtpServerCase):
    def setUp(self):
        # 大きなファイルを残さない。サーバーの停止（super の後始末）より
        # 後に消すため、先に登録する
        self.addCleanup(self._remove_root)
        super().setUp()
        with open(self.real("get.cfg"), "wb") as seed:
            seed.write(os.urandom(SIZE))
        self.events = []
        for signal, kind in ((self.m.transfer_started, "started"),
                             (self.m.transfer_complete, "complete"),
                             (self.m.transfer_interrupted, "interrupted")):
            slot = (lambda *args, kind=kind: self.events.append((kind, args[1])))
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)

    def _remove_root(self):
        root = getattr(self, "root", None)
        if root:
            shutil.rmtree(root, ignore_errors=True)

    def _pump(self, seconds=0.3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def _download_rows(self):
        return [key for key in list(self.m._tx) if key[2] == "download"]

    def _sync(self, ftp):
        """直前のコマンドの処理を、応答の後の台帳の更新まで待つ。

        サーバーは応答（150 / 226 など）を送ってから台帳を更新するので、
        応答を受けただけでは更新が済んでいないことがある。同じ制御接続の
        次のコマンドは、前のコマンドの処理を終えてから読まれる
        """
        self.assertTrue(ftp.sendcmd("NOOP").startswith("200"))

    def _queue_retr(self, ftp):
        """データ接続を張らずに RETR だけを受けさせる"""
        ftp.sendcmd("PASV")
        resp = ftp.sendcmd("RETR get.cfg")
        self.assertTrue(resp.startswith("150"), resp)
        self._sync(ftp)

    def _drop_queue(self, ftp, how):
        """待ち行列の RETR を ABOR / REIN で捨て、片付けまで待つ"""
        resp = ftp.sendcmd(how)
        self.assertTrue(resp.startswith("225" if how == "ABOR" else "230"), resp)
        self._sync(ftp)
        self._pump()

    def _start_download(self, ftp):
        """取得を始めて最初の塊だけ読んだデータ接続を返す（転送中のまま）"""
        data = ftp.transfercmd("RETR get.cfg")
        self.addCleanup(self._close_socket, data)
        first = data.recv(65536)
        self.assertTrue(first)
        self._sync(ftp)     # 転送中でも NOOP には答える（226 はまだ来ない）
        return data, len(first)

    def _finish_download(self, ftp, data, got):
        data.settimeout(10)
        while True:
            chunk = data.recv(1 << 20)
            if not chunk:
                break
            got += len(chunk)
        data.close()
        self.assertTrue(ftp.voidresp().startswith("226"))
        self.assertEqual(got, SIZE)

    def _download_all(self, ftp):
        got = [0]
        resp = ftp.retrbinary("RETR get.cfg",
                              lambda block: got.__setitem__(0, got[0] + len(block)))
        self.assertTrue(resp.startswith("226"), resp)
        self.assertEqual(got[0], SIZE)
        self._sync(ftp)

    def _check_row_being_transferred_by_a_bundled_connection(self, how):
        p1 = self.client()
        self._queue_retr(p1)
        p3 = self.client()
        data, got = self._start_download(p3)     # P1 の行に束ねられる
        self.assertEqual(len(self._download_rows()), 1,
                         "前提: P3 の取得が転送中で、行が開いている")
        self._drop_queue(p1, how)
        self.assertNotIn("interrupted", [kind for kind, _ in self.events],
                         "P3 が取得中の行が P1 の %s で中断になった: %r"
                         % (how, self.events))
        self.assertEqual(len(self._download_rows()), 1,
                         "P3 が取得中の行が台帳から外れた")
        self._finish_download(p3, data, got)
        self._pump()
        self.assertEqual(self.events, [("started", "get.cfg"), ("complete", "get.cfg")])
        self.assertEqual(self.m._tx, {})

    def test_abor_keeps_the_row_a_bundled_connection_is_transferring(self):
        self._check_row_being_transferred_by_a_bundled_connection("ABOR")

    def test_rein_keeps_the_row_a_bundled_connection_is_transferring(self):
        self._check_row_being_transferred_by_a_bundled_connection("REIN")

    def _check_row_reopened_by_another_connection(self, how):
        p1 = self.client()
        self._queue_retr(p1)
        p2 = self.client()
        self._download_all(p2)                   # P1 の行を完了で閉じる
        p3 = self.client()
        data, got = self._start_download(p3)     # 新しい行
        self.assertEqual(len(self._download_rows()), 1,
                         "前提: P3 の取得が転送中で、行が開いている")
        self._drop_queue(p1, how)
        self.assertNotIn("interrupted", [kind for kind, _ in self.events],
                         "作り直された P3 の行が P1 の %s で中断になった: %r"
                         % (how, self.events))
        self._finish_download(p3, data, got)
        self._pump()
        self.assertEqual(self.events, [("started", "get.cfg"), ("complete", "get.cfg"),
                                       ("started", "get.cfg"), ("complete", "get.cfg")])
        self.assertEqual(self.m._tx, {})

    def test_abor_keeps_a_row_reopened_by_another_connection(self):
        self._check_row_reopened_by_another_connection("ABOR")

    def test_rein_keeps_a_row_reopened_by_another_connection(self):
        self._check_row_reopened_by_another_connection("REIN")

    def test_abor_after_another_connection_closed_the_row_reports_nothing(self):
        p1 = self.client()
        self._queue_retr(p1)
        p2 = self.client()
        self._download_all(p2)
        self._drop_queue(p1, "ABOR")
        self.assertEqual(self.events, [("started", "get.cfg"), ("complete", "get.cfg")],
                         "閉じた行について中断が通知された")
        self.assertEqual(self.m._tx, {})

    def test_the_last_of_two_queued_connections_closes_the_row(self):
        """対照: 同じ RETR を待ちにした 2 本のうち、先に ABOR した方は行を
        残し、残った方が ABOR した時点で中断で閉じる"""
        p1 = self.client()
        self._queue_retr(p1)
        p4 = self.client()
        self._queue_retr(p4)                     # P1 の行に束ねられる
        self._drop_queue(p1, "ABOR")
        self.assertEqual(self.events, [("started", "get.cfg")])
        self.assertEqual(len(self._download_rows()), 1)
        self._drop_queue(p4, "ABOR")
        self.assertEqual(self.events, [("started", "get.cfg"), ("interrupted", "get.cfg")])
        self.assertEqual(self.m._tx, {})

    def test_a_connection_that_left_does_not_keep_the_row_open(self):
        """対照: 同じ RETR を待ちにしたまま切断した接続（機器のプローブ）は
        数えない。残った P1 が ABOR すれば、行は中断で閉じる"""
        p0 = self.client()
        self._queue_retr(p0)
        self.assertTrue(p0.quit().startswith("221"))
        p1 = self.client()
        self._queue_retr(p1)                     # P0 の行に束ねられる
        self._drop_queue(p1, "ABOR")
        self.assertEqual(self.events, [("started", "get.cfg"), ("interrupted", "get.cfg")])
        self.assertEqual(self.m._tx, {})


if __name__ == "__main__":
    unittest.main()
